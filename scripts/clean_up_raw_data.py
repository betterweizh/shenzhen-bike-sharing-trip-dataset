"""Merge the raw Shenzhen bike-sharing trips into compact Parquet files.

Every ``bicycle_trip_*.csv`` member of the zip archives in the raw data
directory is streamed, validated and merged. A row is rejected when:

- a datetime column does not match the 24-hour ``YYYY-MM-DD HH:MM:SS``,
  optionally followed by fractional seconds such as ``.0``;
- a coordinate is not a number within the global latitude or longitude
  range;
- ``USER_ID`` is not a lowercase hex prefix followed by exactly ten
  ``*`` mask characters.

Invalid datetime and ``USER_ID`` values are logged one by one as
warnings, and every rejected row is written to a CSV file with its
source file, data row and reason. Fractional seconds are rounded to the
nearest whole second, half up. Exact duplicate rows are dropped and the
trips are sorted by ``START_TIME``.

The raw coordinates are assumed to be GCJ-02, the offset system of
Chinese web maps. The start and end points of every row that passes the
checks above are converted to WGS84 by inverting the GCJ-02 offset with
``GCJ02_TO_WGS84_ITERATIONS`` fixed-point steps. Unlike the usual
implementations, every point is converted, including points outside
China. A row is rejected when a converted point is not finite, falls
outside the global range (near ±180° longitude) or misses its GCJ-02
input by more than ``MAX_INVERSION_ERROR_M`` (the iteration diverges
near the poles). Per file, the median and 99th percentile of the shift
and the largest inversion error are logged in metres. The trip file
records its coordinate system as ``WGS84`` in its metadata
(``DataFrame.attrs``), which the later scripts check.

To save space, coordinates are stored as float32 (the rounding error is
logged), and ``USER_ID`` is replaced by an int32 ``USER_KEY`` whose
mapping to the unmasked hex prefix is written to a separate users file.
``COM_ID`` is dropped: only two files have it, and its value is
constant. The log is written to the console and to a file next to the
trip output.

The script takes no command-line arguments; edit the constants below
to change the input and output paths.
"""

import logging
import math
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

RAW_DIR = Path('data/raw')
TRIPS_OUTPUT = Path('data/processed/bicycle_trips.parquet')
USERS_OUTPUT = Path('data/processed/bicycle_users.parquet')
REJECTED_OUTPUT = Path('data/processed/bicycle_trips_rejected.csv')
LOG_PATH = TRIPS_OUTPUT.parent / 'clean_up_raw_data.log'

# Fixed-point steps of the GCJ-02 to WGS84 inversion. In Shenzhen each
# step shrinks the error by about 2.5-3 orders of magnitude (600 m,
# 1.4 m, 2.5 mm, 0.004 mm), so three steps reach about 0.03 mm and two
# would leave up to about 8 mm.
GCJ02_TO_WGS84_ITERATIONS = 3
# A converted point that misses its GCJ-02 input by more than this has
# not converged (near the poles the iteration diverges) and is rejected.
MAX_INVERSION_ERROR_M = 0.01

# COM_ID is left out: only 2 of the 24 files have it, and there it is
# always '0755**' or empty, so it would only hide duplicates.
_COLUMNS = [
    'START_TIME',
    'START_LAT',
    'START_LNG',
    'END_TIME',
    'END_LAT',
    'END_LNG',
    'USER_ID',
]
_TIME_COLUMNS = [
    'START_TIME',
    'END_TIME',
]
_LAT_COLUMNS = [
    'START_LAT',
    'END_LAT',
]
_LNG_COLUMNS = [
    'START_LNG',
    'END_LNG',
]
_USER_ID_COLUMN = 'USER_ID'
_USER_KEY_COLUMN = 'USER_KEY'
_SORT_COLUMN = 'START_TIME'
_REJECT_REASON_COLUMN = 'REJECT_REASON'
_SOURCE_FILE_COLUMN = 'SOURCE_FILE'
_DATA_ROW_COLUMN = 'DATA_ROW'
# %H is the 24-hour clock (00-23), so a 12-hour time with AM/PM fails.
_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
# Most files write times as '2021-03-30 08:52:13.0'. Such values are
# parsed with this format first, which also reads a bare trailing '.'
# as '.0'; the values it fails on are retried without a fraction.
_FRACTIONAL_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
_ROUND_UP_REMAINDER = pd.Timedelta(milliseconds=500)
_MAX_ROUNDING_EXAMPLES = 3
_MAX_ABS_LAT = 90.0
_MAX_ABS_LNG = 180.0
_MASK_SUFFIX = '*' * 10
_USER_ID_PATTERN = r'[0-9a-f]+\*{10}'
_TRIP_MEMBER_PREFIX = 'bicycle_trip_'
_PARQUET_ROW_GROUP_SIZE = 1_000_000
_PARQUET_COMPRESSION = 'zstd'
# Krasovsky 1940 ellipsoid used by the GCJ-02 offset.
_GCJ02_SEMI_MAJOR_AXIS_M = 6378245.0
_GCJ02_ECCENTRICITY_SQUARED = 0.00669342162296594323
# The GCJ-02 offset polynomials are centred on 105° E, 35° N.
_GCJ02_ORIGIN_LNG = 105.0
_GCJ02_ORIGIN_LAT = 35.0
_EARTH_RADIUS_KM = 6371.0088
_METRES_PER_KM = 1000
_SHIFT_PERCENTILE = 99
# Metadata key, read back as DataFrame.attrs by the later scripts.
_COORDINATE_SYSTEM_ATTR = 'coordinate_system'
_OUTPUT_COORDINATE_SYSTEM = 'WGS84'


@dataclass(frozen=True, slots=True, kw_only=True)
class _TimeReport:
    """Parsing outcome of one datetime column.

    Parameters
    ----------
    fraction_count : int
        Number of valid values that had decimal seconds.
    changed_count : int
        Number of those values with a non-zero fraction, whose value
        the rounding changed.
    examples : tuple of tuple of (str, str)
        Up to a few changed values as (original text, rounded text).
    invalid_values : pandas.Series
        Original text of every invalid or missing value, indexed by
        row label.
    """

    fraction_count: int
    changed_count: int
    examples: tuple[tuple[str, str], ...]
    invalid_values: pd.Series


@dataclass(frozen=True, slots=True, kw_only=True)
class _ConversionReport:
    """GCJ-02 to WGS84 outcome of one point pair in one file.

    Parameters
    ----------
    converted_count : int
        Number of points converted, among the rows that passed the
        earlier checks.
    failed_count : int
        Number of such points whose conversion failed; their rows are
        rejected.
    median_shift_m, p99_shift_m : float
        Median and 99th percentile of the shift of the converted
        points, in metres; NaN when none were converted.
    max_error_m : float
        Largest inversion error of the converted points, in metres;
        NaN when none were converted.
    """

    converted_count: int
    failed_count: int
    median_shift_m: float
    p99_shift_m: float
    max_error_m: float


@dataclass(frozen=True, slots=True, kw_only=True)
class _ValidationResult:
    """Valid and rejected rows of one trip table.

    Parameters
    ----------
    valid : pandas.DataFrame
        Rows that passed every check, with parsed datetime columns,
        WGS84 float columns and ``USER_ID`` reduced to its unmasked
        prefix.
    rejected : pandas.DataFrame
        Rows that failed a check, with their original string values,
        the source file, the data row and a ``REJECT_REASON`` column
        naming the first failed check.
    time_reports : dict of str to _TimeReport
        Parsing outcome per datetime column.
    invalid_user_ids : pandas.Series
        Original text of every invalid or missing ``USER_ID``, indexed
        by row label.
    conversion_reports : dict of str to _ConversionReport
        GCJ-02 to WGS84 outcome per point pair, keyed
        ``'<lat column>/<lng column>'``.
    """

    valid: pd.DataFrame
    rejected: pd.DataFrame
    time_reports: dict[str, _TimeReport]
    invalid_user_ids: pd.Series
    conversion_reports: dict[str, _ConversionReport]


@dataclass(frozen=True, slots=True, kw_only=True)
class _OutputPaths:
    """Destinations of the cleaned data.

    Parameters
    ----------
    trips : pathlib.Path
        Parquet file of the merged trips.
    users : pathlib.Path
        Parquet file mapping ``USER_KEY`` to the unmasked ``USER_ID``.
    rejected : pathlib.Path
        CSV file of the rejected rows.
    """

    trips: Path
    users: Path
    rejected: Path


class _BadLineCounter:
    """Count and skip CSV lines with the wrong number of fields."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, bad_line: object) -> str:
        """Record one bad line and tell the parser to skip it."""
        del bad_line
        self.count += 1
        return 'skip'


def main() -> int:
    """Clean and merge the raw trips using the paths set at the top.

    Returns
    -------
    int
        Process exit code: 0 on success, 1 when the raw data directory
        is missing or holds no zip archives.
    """
    output_paths = _OutputPaths(
        trips=TRIPS_OUTPUT,
        users=USERS_OUTPUT,
        rejected=REJECTED_OUTPUT,
    )
    _configure_logging(LOG_PATH)
    if not RAW_DIR.is_dir():
        logger.error('raw data directory not found: %s', RAW_DIR)
        return 1
    archive_paths = sorted(RAW_DIR.glob('*.zip'))
    if not archive_paths:
        logger.error('no zip archives found in %s', RAW_DIR)
        return 1

    valid_parts: list[pd.DataFrame] = []
    rejected_parts: list[pd.DataFrame] = []
    reject_counts: Counter[str] = Counter()
    check_totals: Counter[str] = Counter()
    rows_read = 0
    bad_line_total = 0
    for archive_path, member in _iter_trip_csvs(archive_paths):
        trips, bad_line_count = _read_trip_csv(archive_path, member)
        result = _validate_trips(trips, member)
        file_reject_counts = Counter(result.rejected[_REJECT_REASON_COLUMN])
        logger.info(
            '%s: %d rows, %d bad lines, %d rejected %s',
            member,
            len(trips),
            bad_line_count,
            len(result.rejected),
            dict(file_reject_counts),
        )
        check_totals.update(_log_time_reports(member, result.time_reports))
        _log_invalid_values(member, _USER_ID_COLUMN, result.invalid_user_ids)
        check_totals[f'{_USER_ID_COLUMN} invalid'] += len(result.invalid_user_ids)
        check_totals.update(
            _log_conversion_reports(member, result.conversion_reports)
        )
        rows_read += len(trips)
        bad_line_total += bad_line_count
        reject_counts.update(file_reject_counts)
        valid_parts.append(result.valid)
        rejected_parts.append(result.rejected)

    trips = _merge_trips(valid_parts)
    rejected = pd.concat(
        rejected_parts,
        ignore_index=True,
    )
    _write_outputs(trips, rejected, output_paths)

    logger.info(
        'summary: %d rows read, %d bad lines, %d rejected %s, %d rows written',
        rows_read,
        bad_line_total,
        len(rejected),
        dict(reject_counts),
        len(trips),
    )
    logger.info('check summary: %s', dict(check_totals))
    if not trips.empty:
        logger.info(
            '%s range: %s to %s',
            _SORT_COLUMN,
            trips[_SORT_COLUMN].iloc[0],
            trips[_SORT_COLUMN].iloc[-1],
        )
    return 0


def _configure_logging(log_path: Path) -> None:
    """Send log records to the console and to a fresh log file."""
    log_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                log_path,
                mode='w',
                encoding='utf-8',
            ),
        ],
    )
    logger.info('writing log to %s', log_path)


def _iter_trip_csvs(archive_paths: list[Path]) -> Iterator[tuple[Path, str]]:
    """Yield every trip CSV member of the given zip archives.

    Parameters
    ----------
    archive_paths : list of pathlib.Path
        Zip archives to scan, in the order they are read.

    Yields
    ------
    tuple of (pathlib.Path, str)
        The archive path and the name of a ``bicycle_trip_*.csv``
        member. The ``logs_trip_*.csv`` scraper logs are skipped.
    """
    for archive_path in archive_paths:
        with zipfile.ZipFile(archive_path) as archive:
            member_names = sorted(archive.namelist())
        for member_name in member_names:
            file_name = member_name.rsplit('/', maxsplit=1)[-1]
            if (
                file_name.startswith(_TRIP_MEMBER_PREFIX)
                and file_name.endswith('.csv')
            ):
                yield archive_path, member_name


def _read_trip_csv(archive_path: Path, member: str) -> tuple[pd.DataFrame, int]:
    """Read one trip CSV from an archive with every column as a string.

    Parameters
    ----------
    archive_path : pathlib.Path
        Zip archive holding the member.
    member : str
        Name of the CSV member inside the archive.

    Returns
    -------
    trips : pandas.DataFrame
        The trip rows, all columns as strings and unparsed.
    bad_line_count : int
        Number of lines skipped because they had the wrong number of
        fields.
    """
    bad_line_counter = _BadLineCounter()
    with (
        zipfile.ZipFile(archive_path) as archive,
        archive.open(member) as csv_file,
    ):
        trips = pd.read_csv(
            csv_file,
            engine='pyarrow',
            usecols=_COLUMNS,
            dtype=str,
            on_bad_lines=bad_line_counter,
        )
    return trips, bad_line_counter.count


def _validate_trips(trips: pd.DataFrame, member: str) -> _ValidationResult:
    """Check the datetime, coordinate and user columns of a trip table.

    A datetime is valid when it matches the 24-hour
    ``YYYY-MM-DD HH:MM:SS``, optionally followed by fractional seconds,
    which are rounded to the nearest whole second. A coordinate is valid
    when it is a number with an absolute value of at most 90 for
    latitude and 180 for longitude. A ``USER_ID`` is valid when it is a
    lowercase hex prefix followed by exactly ten ``*``; the stars are
    removed from valid IDs. Missing values fail. Last, the GCJ-02 start
    and end points are converted to WGS84, and a row fails when a
    conversion fails (see ``_conversion_succeeded``).

    Parameters
    ----------
    trips : pandas.DataFrame
        Trip rows with all columns as strings, as read by
        ``_read_trip_csv``.
    member : str
        Name of the CSV member the rows came from, recorded with the
        rejected rows.

    Returns
    -------
    _ValidationResult
        The valid rows with parsed columns, the rejected rows with
        their original values, source and first failed check, the
        parsing outcome per datetime column, the invalid user IDs and
        the conversion outcome per point pair.
    """
    parsed_columns: dict[str, pd.Series] = {}
    time_reports: dict[str, _TimeReport] = {}
    reject_reason = pd.Series(pd.NA, index=trips.index, dtype='str')
    for column in _TIME_COLUMNS:
        parsed_time, time_reports[column] = _parse_times(trips[column])
        parsed_columns[column] = parsed_time
        _record_failures(reject_reason, parsed_time.isna(), column)
    for column in _LAT_COLUMNS + _LNG_COLUMNS:
        max_abs_value = _MAX_ABS_LAT if column in _LAT_COLUMNS else _MAX_ABS_LNG
        parsed_value = pd.to_numeric(trips[column], errors='coerce')
        parsed_columns[column] = parsed_value
        is_out_of_range = ~parsed_value.abs().le(max_abs_value)
        _record_failures(reject_reason, is_out_of_range, column)

    user_ids = trips[_USER_ID_COLUMN]
    is_valid_user = (
        user_ids
        .str
        .fullmatch(_USER_ID_PATTERN)
        .fillna(False)
        .astype(bool)
    )
    parsed_columns[_USER_ID_COLUMN] = user_ids.str.removesuffix(_MASK_SUFFIX)
    _record_failures(reject_reason, ~is_valid_user, _USER_ID_COLUMN)

    conversion_reports: dict[str, _ConversionReport] = {}
    for lat_column, lng_column in zip(_LAT_COLUMNS, _LNG_COLUMNS, strict=True):
        gcj_lat = parsed_columns[lat_column].to_numpy(dtype=np.float64)
        gcj_lng = parsed_columns[lng_column].to_numpy(dtype=np.float64)
        wgs_lat, wgs_lng, error_m = _gcj02_to_wgs84(gcj_lat, gcj_lng)
        is_converted = _conversion_succeeded(wgs_lat, wgs_lng, error_m)
        # Rows that passed every earlier check; the others were not
        # meant to be converted and keep their first reject reason.
        is_candidate = reject_reason.isna().to_numpy()
        pair_name = f'{lat_column}/{lng_column}'
        _record_failures(
            reject_reason,
            ~is_converted,
            f'{pair_name} after GCJ-02 to WGS84 conversion',
        )
        is_reported = is_candidate & is_converted
        conversion_reports[pair_name] = _conversion_report(
            gcj_lat[is_reported],
            gcj_lng[is_reported],
            wgs_lat[is_reported],
            wgs_lng[is_reported],
            error_m[is_reported],
            failed_count=int((is_candidate & ~is_converted).sum()),
        )
        parsed_columns[lat_column] = pd.Series(wgs_lat, index=trips.index)
        parsed_columns[lng_column] = pd.Series(wgs_lng, index=trips.index)

    is_rejected = reject_reason.notna()
    rejected = trips[is_rejected].copy()
    rejected.insert(0, _SOURCE_FILE_COLUMN, member)
    rejected.insert(1, _DATA_ROW_COLUMN, rejected.index + 1)
    rejected[_REJECT_REASON_COLUMN] = reject_reason[is_rejected]

    valid = trips[~is_rejected].copy()
    for column, parsed_column in parsed_columns.items():
        valid[column] = parsed_column[~is_rejected]
    valid = valid.astype(
        {
            'START_TIME': 'datetime64[s]',
            'END_TIME': 'datetime64[s]',
            'START_LAT': 'float64',
            'START_LNG': 'float64',
            'END_LAT': 'float64',
            'END_LNG': 'float64',
            'USER_ID': 'str',
        }
    )
    return _ValidationResult(
        valid=valid,
        rejected=rejected,
        time_reports=time_reports,
        invalid_user_ids=user_ids[~is_valid_user],
        conversion_reports=conversion_reports,
    )


def _parse_times(raw_times: pd.Series) -> tuple[pd.Series, _TimeReport]:
    """Parse datetime text and round fractional seconds.

    Values are parsed as ``YYYY-MM-DD HH:MM:SS.ffffff`` first, which
    takes up to six fraction digits and reads a bare trailing ``.`` as
    zero. The values that fail are parsed as ``YYYY-MM-DD HH:MM:SS``.
    ``HH`` is the 24-hour clock. A remainder of half a second or more
    rounds up to the next second.

    Parameters
    ----------
    raw_times : pandas.Series of str
        Datetime text as read from the CSV.

    Returns
    -------
    rounded_times : pandas.Series of datetime64
        Times rounded to whole seconds, NaT where the text is missing
        or invalid.
    report : _TimeReport
        Counts of values with decimals and of values the rounding
        changed, examples, and the invalid values.
    """
    fractional_times = pd.to_datetime(
        raw_times,
        format=_FRACTIONAL_DATETIME_FORMAT,
        errors='coerce',
    )
    has_fraction = fractional_times.notna()
    whole_second_times = pd.to_datetime(
        raw_times[~has_fraction],
        format=_DATETIME_FORMAT,
        errors='coerce',
    )
    parsed_times = fractional_times.combine_first(whole_second_times)

    floored_times = parsed_times.dt.floor('s')
    is_rounded_up = (parsed_times - floored_times).ge(_ROUND_UP_REMAINDER)
    rounded_times = floored_times + pd.to_timedelta(
        is_rounded_up.astype('int64'),
        unit='s',
    )

    is_changed = has_fraction & rounded_times.ne(parsed_times)
    changed_labels = is_changed[is_changed].index[:_MAX_ROUNDING_EXAMPLES]
    examples = tuple(
        (raw_times[row_label], str(rounded_times[row_label]))
        for row_label in changed_labels
    )
    report = _TimeReport(
        fraction_count=int(has_fraction.sum()),
        changed_count=int(is_changed.sum()),
        examples=examples,
        invalid_values=raw_times[rounded_times.isna()],
    )
    return rounded_times, report


def _log_time_reports(
    member: str,
    time_reports: dict[str, _TimeReport],
) -> Counter[str]:
    """Log the datetime parsing outcome of one file.

    Parameters
    ----------
    member : str
        Name of the CSV member, used in the log messages.
    time_reports : dict of str to _TimeReport
        Parsing outcome per datetime column.

    Returns
    -------
    collections.Counter of str
        Counts keyed ``'<column> invalid'``, ``'<column> with
        decimals'`` and ``'<column> changed by rounding'``, for totals.
    """
    counts: Counter[str] = Counter()
    for column, report in time_reports.items():
        _log_invalid_values(member, column, report.invalid_values)
        logger.info(
            '%s: %s %d invalid, %d values with decimal seconds, '
            '%d changed by rounding, examples (original -> rounded) %s',
            member,
            column,
            len(report.invalid_values),
            report.fraction_count,
            report.changed_count,
            list(report.examples),
        )
        counts[f'{column} invalid'] += len(report.invalid_values)
        counts[f'{column} with decimals'] += report.fraction_count
        counts[f'{column} changed by rounding'] += report.changed_count
    return counts


def _log_invalid_values(
    member: str,
    column: str,
    invalid_values: pd.Series,
) -> None:
    """Log every invalid value of one column as a warning.

    Parameters
    ----------
    member : str
        Name of the CSV member, used in the log messages.
    column : str
        Name of the checked column.
    invalid_values : pandas.Series
        Original text of the invalid values, indexed by row label. The
        data row in the message counts from 1 after the header and
        skips bad lines.
    """
    for row_label, raw_value in invalid_values.items():
        logger.warning(
            '%s: invalid %s at data row %d: %r',
            member,
            column,
            row_label + 1,
            raw_value,
        )


def _record_failures(
    reject_reason: pd.Series,
    has_failed: pd.Series | np.ndarray,
    column: str,
) -> None:
    """Set the reject reason of newly failed rows in place.

    Parameters
    ----------
    reject_reason : pandas.Series
        Reason per row, missing for rows that have passed so far. Rows
        that already have a reason keep it, so the first failed check
        is recorded.
    has_failed : pandas.Series or numpy.ndarray of bool
        Whether each row failed the check on ``column``.
    column : str
        Name of the checked column, used in the reason text.
    """
    is_new_failure = has_failed & reject_reason.isna()
    reject_reason[is_new_failure] = f'invalid {column}'


def _merge_trips(valid_parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate, deduplicate and sort the valid trip tables.

    Duplicates are found on the full-precision values, before
    ``_compact_trips`` narrows the types.

    Parameters
    ----------
    valid_parts : list of pandas.DataFrame
        Validated trip tables with parsed columns.

    Returns
    -------
    pandas.DataFrame
        All unique trips sorted by ``START_TIME`` with a fresh index.
        Rows with equal start times keep their read order.
    """
    trips = pd.concat(
        valid_parts,
        ignore_index=True,
    )
    row_count = len(trips)
    trips = trips.drop_duplicates()
    logger.info('dropped %d exact duplicate rows', row_count - len(trips))
    trips = trips.sort_values(
        _SORT_COLUMN,
        kind='stable',
    )
    return trips.reset_index(drop=True)


def _gcj02_to_wgs84(
    gcj_lat: np.ndarray,
    gcj_lng: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert points from GCJ-02 to WGS84 by fixed-point iteration.

    The inverse has no closed form, so it is found iteratively:
    starting from the GCJ-02 point, each of the
    ``GCJ02_TO_WGS84_ITERATIONS`` steps subtracts the offset evaluated
    at the current WGS84 estimate. Points outside China are converted
    like any other. Near the poles the iteration diverges and can give
    huge or non-finite values, which ``_conversion_succeeded`` catches.

    Parameters
    ----------
    gcj_lat, gcj_lng : numpy.ndarray of float
        GCJ-02 latitude and longitude in degrees, the same shape; NaN
        gives NaN.

    Returns
    -------
    wgs_lat, wgs_lng : numpy.ndarray of float
        WGS84 latitude and longitude in degrees.
    error_m : numpy.ndarray of float
        Inversion error in metres: the distance between the WGS84 point
        shifted back to GCJ-02 and the GCJ-02 input.
    """
    # Diverging points overflow or turn into NaN; they are rejected
    # afterwards, so the floating-point warnings are only noise.
    with np.errstate(
        over='ignore',
        invalid='ignore',
        divide='ignore',
    ):
        wgs_lat = gcj_lat.copy()
        wgs_lng = gcj_lng.copy()
        for _ in range(GCJ02_TO_WGS84_ITERATIONS):
            lat_offset, lng_offset = _gcj02_offset(wgs_lat, wgs_lng)
            wgs_lat = gcj_lat - lat_offset
            wgs_lng = gcj_lng - lng_offset
        lat_offset, lng_offset = _gcj02_offset(wgs_lat, wgs_lng)
        error_m = _METRES_PER_KM * _haversine_km(
            gcj_lat,
            gcj_lng,
            wgs_lat + lat_offset,
            wgs_lng + lng_offset,
        )
    return wgs_lat, wgs_lng, error_m


def _conversion_succeeded(
    wgs_lat: np.ndarray,
    wgs_lng: np.ndarray,
    error_m: np.ndarray,
) -> np.ndarray:
    """Tell which converted points are usable.

    Parameters
    ----------
    wgs_lat, wgs_lng, error_m : numpy.ndarray of float
        Output of ``_gcj02_to_wgs84``.

    Returns
    -------
    numpy.ndarray of bool
        True where the point is finite, within the global latitude and
        longitude range, and within ``MAX_INVERSION_ERROR_M`` of its
        GCJ-02 input. NaN fails every test.
    """
    with np.errstate(invalid='ignore'):
        return (
            (np.abs(wgs_lat) <= _MAX_ABS_LAT)
            & (np.abs(wgs_lng) <= _MAX_ABS_LNG)
            & (error_m <= MAX_INVERSION_ERROR_M)
        )


def _conversion_report(
    gcj_lat: np.ndarray,
    gcj_lng: np.ndarray,
    wgs_lat: np.ndarray,
    wgs_lng: np.ndarray,
    error_m: np.ndarray,
    *,
    failed_count: int,
) -> _ConversionReport:
    """Summarize the shift and inversion error of converted points.

    Parameters
    ----------
    gcj_lat, gcj_lng, wgs_lat, wgs_lng : numpy.ndarray of float
        GCJ-02 inputs and WGS84 results, in degrees, of the points that
        converted successfully.
    error_m : numpy.ndarray of float
        Inversion error of the same points in metres.
    failed_count : int
        Number of points whose conversion failed.

    Returns
    -------
    _ConversionReport
        Counts, shift percentiles and the largest error; the statistics
        are NaN when no point was converted.
    """
    if not error_m.size:
        return _ConversionReport(
            converted_count=0,
            failed_count=failed_count,
            median_shift_m=float('nan'),
            p99_shift_m=float('nan'),
            max_error_m=float('nan'),
        )
    shift_m = _METRES_PER_KM * _haversine_km(gcj_lat, gcj_lng, wgs_lat, wgs_lng)
    median_shift_m, p99_shift_m = np.percentile(shift_m, [50, _SHIFT_PERCENTILE])
    return _ConversionReport(
        converted_count=len(error_m),
        failed_count=failed_count,
        median_shift_m=float(median_shift_m),
        p99_shift_m=float(p99_shift_m),
        max_error_m=float(error_m.max()),
    )


def _log_conversion_reports(
    member: str,
    conversion_reports: dict[str, _ConversionReport],
) -> Counter[str]:
    """Log the GCJ-02 to WGS84 outcome of one file.

    Parameters
    ----------
    member : str
        Name of the CSV member, used in the log messages.
    conversion_reports : dict of str to _ConversionReport
        Conversion outcome per point pair.

    Returns
    -------
    collections.Counter of str
        Counts keyed ``'<pair> converted'`` and ``'<pair> failed
        conversion'``, for totals.
    """
    counts: Counter[str] = Counter()
    for pair_name, report in conversion_reports.items():
        logger.info(
            '%s: %s GCJ-02 to WGS84 on %d points, %d failed: shift median '
            '%.1f m, p%d %.1f m; inversion error max %.2e m',
            member,
            pair_name,
            report.converted_count,
            report.failed_count,
            report.median_shift_m,
            _SHIFT_PERCENTILE,
            report.p99_shift_m,
            report.max_error_m,
        )
        counts[f'{pair_name} converted'] += report.converted_count
        counts[f'{pair_name} failed conversion'] += report.failed_count
    return counts


def _gcj02_offset(
    lat: np.ndarray,
    lng: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute the GCJ-02 offset that is added to WGS84 points.

    Parameters
    ----------
    lat, lng : numpy.ndarray of float
        WGS84 latitude and longitude in degrees, the same shape.

    Returns
    -------
    lat_offset, lng_offset : numpy.ndarray of float
        Offset in degrees, so that GCJ-02 = WGS84 + offset.
    """
    x = lng - _GCJ02_ORIGIN_LNG
    y = lat - _GCJ02_ORIGIN_LAT
    sqrt_abs_x = np.sqrt(np.abs(x))
    # Both polynomials share this term in x.
    shared_term = 2 / 3 * (20 * np.sin(6 * math.pi * x) + 20 * np.sin(2 * math.pi * x))
    lat_shift = (
        -100
        + 2 * x
        + 3 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * sqrt_abs_x
        + shared_term
        + 2 / 3 * (20 * np.sin(math.pi * y) + 40 * np.sin(math.pi * y / 3))
        + 2 / 3 * (160 * np.sin(math.pi * y / 12) + 320 * np.sin(math.pi * y / 30))
    )
    lng_shift = (
        300
        + x
        + 2 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * sqrt_abs_x
        + shared_term
        + 2 / 3 * (20 * np.sin(math.pi * x) + 40 * np.sin(math.pi * x / 3))
        + 2 / 3 * (150 * np.sin(math.pi * x / 12) + 300 * np.sin(math.pi * x / 30))
    )
    lat_rad = np.radians(lat)
    curvature = 1 - _GCJ02_ECCENTRICITY_SQUARED * np.sin(lat_rad) ** 2
    sqrt_curvature = np.sqrt(curvature)
    # Shifts are in metres; divide by the meridian and parallel radii
    # of the Krasovsky ellipsoid to get degrees.
    meridian_radius = (
        _GCJ02_SEMI_MAJOR_AXIS_M
        * (1 - _GCJ02_ECCENTRICITY_SQUARED)
        / (curvature * sqrt_curvature)
    )
    parallel_radius = _GCJ02_SEMI_MAJOR_AXIS_M / sqrt_curvature * np.cos(lat_rad)
    lat_offset = np.degrees(lat_shift / meridian_radius)
    lng_offset = np.degrees(lng_shift / parallel_radius)
    return lat_offset, lng_offset


def _haversine_km(
    start_lat: np.ndarray,
    start_lng: np.ndarray,
    end_lat: np.ndarray,
    end_lng: np.ndarray,
) -> np.ndarray:
    """Compute the great-circle distance between points in kilometres.

    Parameters
    ----------
    start_lat, start_lng : numpy.ndarray of float
        Start latitude and longitude in degrees.
    end_lat, end_lng : numpy.ndarray of float
        End latitude and longitude in degrees, the same shape as the
        start arrays.

    Returns
    -------
    numpy.ndarray of float
        Distance per point pair on a sphere of mean Earth radius.
    """
    start_lat_rad = np.radians(start_lat)
    end_lat_rad = np.radians(end_lat)
    lat_change = end_lat_rad - start_lat_rad
    lng_change = np.radians(end_lng - start_lng)
    half_chord = (
        np.sin(lat_change / 2) ** 2
        + np.cos(start_lat_rad) * np.cos(end_lat_rad) * np.sin(lng_change / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.minimum(half_chord, 1.0)))


def _compact_trips(trips: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Narrow the trip columns to save storage.

    Coordinates become float32, and the mean and maximum rounding error
    of each coordinate column is logged in degrees. ``USER_ID`` is
    replaced by an int32 ``USER_KEY`` numbered in sorted ``USER_ID``
    order, so keys are stable across runs on the same data.

    Parameters
    ----------
    trips : pandas.DataFrame
        Merged trips with float64 coordinates and unmasked ``USER_ID``
        strings, none missing.

    Returns
    -------
    compact_trips : pandas.DataFrame
        The trips with float32 coordinates and ``USER_KEY`` in place of
        ``USER_ID``.
    users : pandas.DataFrame
        One row per user with ``USER_KEY`` and ``USER_ID``.
    """
    compact_trips = trips.copy()
    for column in _LAT_COLUMNS + _LNG_COLUMNS:
        full_values = trips[column].to_numpy()
        narrow_values = full_values.astype(np.float32)
        rounding_error = np.abs(narrow_values.astype(np.float64) - full_values)
        logger.info(
            '%s float32 rounding error: mean %.3e deg, max %.3e deg',
            column,
            rounding_error.mean() if rounding_error.size else 0.0,
            rounding_error.max() if rounding_error.size else 0.0,
        )
        compact_trips[column] = narrow_values

    user_codes, user_ids = pd.factorize(trips[_USER_ID_COLUMN], sort=True)
    compact_trips[_USER_ID_COLUMN] = user_codes.astype(np.int32)
    compact_trips = compact_trips.rename(
        columns={_USER_ID_COLUMN: _USER_KEY_COLUMN}
    )
    users = pd.DataFrame(
        {
            _USER_KEY_COLUMN: np.arange(len(user_ids), dtype=np.int32),
            _USER_ID_COLUMN: user_ids,
        }
    )
    logger.info('%d distinct users', len(users))
    return compact_trips, users


def _write_outputs(
    trips: pd.DataFrame,
    rejected: pd.DataFrame,
    output_paths: _OutputPaths,
) -> None:
    """Compact the trips and write the trips, users and rejected rows.

    Parameters
    ----------
    trips : pandas.DataFrame
        Merged, deduplicated and sorted trips from ``_merge_trips``.
    rejected : pandas.DataFrame
        All rejected rows. The CSV is skipped when there are none.
    output_paths : _OutputPaths
        Destinations of the three outputs.
    """
    compact_trips, users = _compact_trips(trips)
    compact_trips.attrs[_COORDINATE_SYSTEM_ATTR] = _OUTPUT_COORDINATE_SYSTEM
    _write_parquet(compact_trips, output_paths.trips)
    _write_parquet(users, output_paths.users)
    if rejected.empty:
        logger.info('no rejected rows, skipping %s', output_paths.rejected)
        return
    output_paths.rejected.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    rejected.to_csv(
        output_paths.rejected,
        index=False,
        encoding='utf-8',
    )
    logger.info('wrote %d rows to %s', len(rejected), output_paths.rejected)


def _write_parquet(table: pd.DataFrame, path: Path) -> None:
    """Write a table to a zstd-compressed Parquet file."""
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    table.to_parquet(
        path,
        engine='pyarrow',
        compression=_PARQUET_COMPRESSION,
        index=False,
        row_group_size=_PARQUET_ROW_GROUP_SIZE,
    )
    logger.info('wrote %d rows to %s', len(table), path)


if __name__ == '__main__':
    raise SystemExit(main())
