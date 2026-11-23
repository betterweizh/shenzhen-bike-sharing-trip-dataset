"""Extract the valid trips of the study period from the processed trips.

Only trips that start between ``STUDY_START`` and ``STUDY_END`` (both
inclusive) are read. This is the stretch of steady, complete daily
coverage found by the temporal exploratory analysis. A trip in the
period is screened out when any of these checks fails:

- **travel time**: the duration (``END_TIME`` minus ``START_TIME``) is
  outside ``MIN_DURATION_MIN`` to ``MAX_DURATION_MIN`` minutes;
- **travel distance**: the straight-line (haversine) distance between
  the start and end points is outside ``MIN_DISTANCE_KM`` to
  ``MAX_DISTANCE_KM``;
- **boundary**: the start or end point lies outside the Shenzhen
  bounding box widened by ``BOUNDARY_BUFFER_KM`` on every side;
- **travel speed**: the straight-line distance divided by the duration
  is outside ``MIN_SPEED_KMH`` to ``MAX_SPEED_KMH``.

The coordinates are screened and written as they are stored in the
input, without any conversion. ``clean_up_raw_data.py`` converts them to
WGS84 and records that in the file's metadata; the script stops with an
error when the input does not say WGS84 (for example, a file written
before the conversion step existed) or when the period holds no trips.

A trip with a missing value fails the checks that use it. Duration,
distance and speed are computed only for screening: the kept trips are
written with the same columns and values as the input, in
``START_TIME`` order.
``USER_KEY`` still joins to ``bicycle_users.parquet``.

The log gives, per check, how many trips fail it (a trip can fail
several) and how many it removes when the checks are applied in the
order above. The same counts are written to a CSV file.

The script takes no command-line arguments; edit the constants below
to change the period, the thresholds or the paths.
"""

import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRIPS_PATH = Path('data/processed/bicycle_trips.parquet')
OUTPUT_PATH = Path('output/selected_trips/bicycle_trips_selected_period.parquet')
SUMMARY_PATH = Path('output/selected_trips/filter_summary.csv')
LOG_PATH = Path('output/selected_trips/bicycle_trips_selected_period.log')

# Both dates are inclusive.
STUDY_START = pd.Timestamp('2021-05-01')
STUDY_END = pd.Timestamp('2021-08-28')

# Trips under a minute are mostly failed unlocks or immediate returns;
# three hours is far beyond a normal bike-share ride.
MIN_DURATION_MIN = 1.0
MAX_DURATION_MIN = 180.0

# Below 100 m the start and end points are within GPS error of each
# other, so the trip cannot be told apart from relocking in place.
MIN_DISTANCE_KM = 0.1
MAX_DISTANCE_KM = 30.0

# Official extent of Shenzhen: 22°27'-22°52' N, 113°46'-114°37' E.
SHENZHEN_MIN_LAT = 22 + 27 / 60
SHENZHEN_MAX_LAT = 22 + 52 / 60
SHENZHEN_MIN_LNG = 113 + 46 / 60
SHENZHEN_MAX_LNG = 114 + 37 / 60
BOUNDARY_BUFFER_KM = 5.0

# Straight-line speed is below the riding speed, so the lower limit is
# loose; above 25 km/h a pedal bike is implausible over a whole trip.
MIN_SPEED_KMH = 1.0
MAX_SPEED_KMH = 25.0

_START_TIME_COLUMN = 'START_TIME'
_END_TIME_COLUMN = 'END_TIME'
_START_LAT_COLUMN = 'START_LAT'
_START_LNG_COLUMN = 'START_LNG'
_END_LAT_COLUMN = 'END_LAT'
_END_LNG_COLUMN = 'END_LNG'

_DURATION_COLUMN = 'DURATION_MIN'
_DISTANCE_COLUMN = 'DISTANCE_KM'
_SPEED_COLUMN = 'SPEED_KMH'

_CHECK_COLUMN = 'CHECK'
_RULE_COLUMN = 'RULE'
_FAILING_COLUMN = 'TRIPS_FAILING'
_REMOVED_COLUMN = 'TRIPS_REMOVED_IN_ORDER'
_REMAINING_COLUMN = 'TRIPS_REMAINING'

# Metadata key written by clean_up_raw_data.py, read as DataFrame.attrs.
_COORDINATE_SYSTEM_ATTR = 'coordinate_system'
_EXPECTED_COORDINATE_SYSTEM = 'WGS84'

_EARTH_RADIUS_KM = 6371.0088
_KM_PER_DEGREE_LAT = math.pi * _EARTH_RADIUS_KM / 180
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_PARQUET_ROW_GROUP_SIZE = 1_000_000
_PARQUET_COMPRESSION = 'zstd'


def main() -> int:
    """Extract and screen the study-period trips.

    Returns
    -------
    int
        Process exit code: 0 on success, 1 when the input is not marked
        as WGS84 or the study period holds no trips.
    """
    _configure_logging(LOG_PATH)
    # Reading no columns loads only the metadata, so a stale file fails
    # before the full read.
    coordinate_system = pd.read_parquet(
        TRIPS_PATH,
        columns=[],
    ).attrs.get(_COORDINATE_SYSTEM_ATTR)
    if coordinate_system != _EXPECTED_COORDINATE_SYSTEM:
        logger.error(
            '%s has coordinate system %r, expected %r; rerun '
            'clean_up_raw_data.py',
            TRIPS_PATH,
            coordinate_system,
            _EXPECTED_COORDINATE_SYSTEM,
        )
        return 1
    trips = _load_study_period(
        TRIPS_PATH,
        STUDY_START,
        STUDY_END,
    )
    if trips.empty:
        logger.error(
            'no trips between %s and %s in %s',
            STUDY_START.date(),
            STUDY_END.date(),
            TRIPS_PATH,
        )
        return 1
    checks = _run_checks(trips, _trip_measures(trips))
    is_valid, summary = _summarize_checks(
        checks,
        f'{STUDY_START.date()} to {STUDY_END.date()}',
    )
    for row in summary.to_dict('records'):
        logger.info(
            '%s (%s): %d failing, %d removed in order, %d remaining',
            row[_CHECK_COLUMN],
            row[_RULE_COLUMN],
            row[_FAILING_COLUMN],
            row[_REMOVED_COLUMN],
            row[_REMAINING_COLUMN],
        )
    valid_trips = trips[is_valid]
    logger.info(
        'kept %d of %d study-period trips (%.2f%%)',
        len(valid_trips),
        len(trips),
        100 * len(valid_trips) / len(trips),
    )

    SUMMARY_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    summary.to_csv(
        SUMMARY_PATH,
        index=False,
    )
    logger.info('wrote %d rows to %s', len(summary), SUMMARY_PATH)
    _write_parquet(valid_trips, OUTPUT_PATH)
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


def _load_study_period(
    trips_path: Path,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> pd.DataFrame:
    """Read the trips that start within the study period.

    Parameters
    ----------
    trips_path : pathlib.Path
        Parquet file written by ``clean_up_raw_data.py``.
    start_date : pandas.Timestamp
        First day of the period, at midnight.
    end_date : pandas.Timestamp
        Last day of the period, at midnight; trips starting on this day
        are included.

    Returns
    -------
    pandas.DataFrame
        The trips of the period in the file's ``START_TIME`` order.
    """
    period_end = end_date + pd.Timedelta(days=1)
    logger.info(
        'reading trips starting %s to %s from %s',
        start_date.date(),
        end_date.date(),
        trips_path,
    )
    trips = pd.read_parquet(
        trips_path,
        filters=[
            (_START_TIME_COLUMN, '>=', start_date),
            (_START_TIME_COLUMN, '<', period_end),
        ],
    )
    logger.info('read %d study-period trips', len(trips))
    return trips


def _trip_measures(trips: pd.DataFrame) -> pd.DataFrame:
    """Compute the duration, straight-line distance and speed of trips.

    The measures are used only for screening and are not saved.

    Parameters
    ----------
    trips : pandas.DataFrame
        Trips with start and end times and coordinates.

    Returns
    -------
    pandas.DataFrame
        Float64 columns ``DURATION_MIN`` (minutes), ``DISTANCE_KM``
        (kilometres) and ``SPEED_KMH`` (kilometres per hour) on the
        trips' index. Speed is NaN or infinite where the duration is
        zero or negative.
    """
    duration = trips[_END_TIME_COLUMN] - trips[_START_TIME_COLUMN]
    duration_min = duration.dt.total_seconds().to_numpy() / _SECONDS_PER_MINUTE
    distance_km = _haversine_km(
        trips[_START_LAT_COLUMN].to_numpy(dtype=np.float64),
        trips[_START_LNG_COLUMN].to_numpy(dtype=np.float64),
        trips[_END_LAT_COLUMN].to_numpy(dtype=np.float64),
        trips[_END_LNG_COLUMN].to_numpy(dtype=np.float64),
    )
    # Zero or negative durations give inf or NaN speeds; the duration
    # check already rejects those trips.
    with np.errstate(
        divide='ignore',
        invalid='ignore',
    ):
        speed_kmh = distance_km / (duration_min / _MINUTES_PER_HOUR)
    return pd.DataFrame(
        {
            _DURATION_COLUMN: duration_min,
            _DISTANCE_COLUMN: distance_km,
            _SPEED_COLUMN: speed_kmh,
        },
        index=trips.index,
    )


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


def _buffered_bounds() -> tuple[float, float, float, float]:
    """Widen the Shenzhen bounding box by the buffer distance.

    The latitude buffer converts kilometres with the length of one
    degree of latitude. The longitude buffer uses the length of one
    degree of longitude at the buffered box's northern edge, where it
    is shortest, so the buffer is at least ``BOUNDARY_BUFFER_KM`` along
    the whole box.

    Returns
    -------
    tuple of float
        Minimum latitude, maximum latitude, minimum longitude and
        maximum longitude, in degrees.
    """
    lat_buffer = BOUNDARY_BUFFER_KM / _KM_PER_DEGREE_LAT
    northern_lat = SHENZHEN_MAX_LAT + lat_buffer
    km_per_degree_lng = _KM_PER_DEGREE_LAT * math.cos(math.radians(northern_lat))
    lng_buffer = BOUNDARY_BUFFER_KM / km_per_degree_lng
    return (
        SHENZHEN_MIN_LAT - lat_buffer,
        SHENZHEN_MAX_LAT + lat_buffer,
        SHENZHEN_MIN_LNG - lng_buffer,
        SHENZHEN_MAX_LNG + lng_buffer,
    )


def _run_checks(
    trips: pd.DataFrame,
    measures: pd.DataFrame,
) -> list[tuple[str, str, pd.Series]]:
    """Evaluate every screening check on the trips.

    Parameters
    ----------
    trips : pandas.DataFrame
        Trips with start and end coordinates in degrees.
    measures : pandas.DataFrame
        Output of ``_trip_measures`` for the same trips.

    Returns
    -------
    list of tuple of (str, str, pandas.Series)
        One ``(name, rule, passes)`` per check, in the order the checks
        are applied: a check name, a readable rule and a boolean series
        that is True where the trip passes. ``between`` is False for
        NaN, so missing values fail.
    """
    min_lat, max_lat, min_lng, max_lng = _buffered_bounds()
    logger.info(
        'buffered Shenzhen box: lat %.4f to %.4f, lng %.4f to %.4f',
        min_lat,
        max_lat,
        min_lng,
        max_lng,
    )
    is_inside = (
        trips[_START_LAT_COLUMN].between(min_lat, max_lat)
        & trips[_START_LNG_COLUMN].between(min_lng, max_lng)
        & trips[_END_LAT_COLUMN].between(min_lat, max_lat)
        & trips[_END_LNG_COLUMN].between(min_lng, max_lng)
    )
    return [
        (
            'travel_time',
            f'{MIN_DURATION_MIN:g} <= duration <= {MAX_DURATION_MIN:g} min',
            measures[_DURATION_COLUMN].between(MIN_DURATION_MIN, MAX_DURATION_MIN),
        ),
        (
            'travel_distance',
            f'{MIN_DISTANCE_KM:g} <= distance <= {MAX_DISTANCE_KM:g} km',
            measures[_DISTANCE_COLUMN].between(MIN_DISTANCE_KM, MAX_DISTANCE_KM),
        ),
        (
            'boundary',
            f'start and end in Shenzhen box + {BOUNDARY_BUFFER_KM:g} km',
            is_inside,
        ),
        (
            'travel_speed',
            f'{MIN_SPEED_KMH:g} <= speed <= {MAX_SPEED_KMH:g} km/h',
            measures[_SPEED_COLUMN].between(MIN_SPEED_KMH, MAX_SPEED_KMH),
        ),
    ]


def _summarize_checks(
    checks: list[tuple[str, str, pd.Series]],
    period_rule: str,
) -> tuple[pd.Series, pd.DataFrame]:
    """Combine the checks and count the trips each one screens out.

    Parameters
    ----------
    checks : list of tuple of (str, str, pandas.Series)
        Output of ``_run_checks``, in the order the checks are applied;
        must not be empty.
    period_rule : str
        Description of the study period for the first summary row.

    Returns
    -------
    is_valid : pandas.Series of bool
        True where the trip passes every check.
    summary : pandas.DataFrame
        A first row for the study period, then one row per check, with
        ``CHECK``, ``RULE``, ``TRIPS_FAILING`` (trips failing this check,
        whatever the others say), ``TRIPS_REMOVED_IN_ORDER`` (trips this
        check removes after the earlier checks) and ``TRIPS_REMAINING``.
    """
    is_valid = pd.Series(True, index=checks[0][2].index)
    remaining = len(is_valid)
    rows = [
        {
            _CHECK_COLUMN: 'study_period',
            _RULE_COLUMN: period_rule,
            _FAILING_COLUMN: 0,
            _REMOVED_COLUMN: 0,
            _REMAINING_COLUMN: remaining,
        }
    ]
    for check_name, rule, passes in checks:
        is_valid = is_valid & passes
        remaining_after = int(is_valid.sum())
        rows.append(
            {
                _CHECK_COLUMN: check_name,
                _RULE_COLUMN: rule,
                _FAILING_COLUMN: int((~passes).sum()),
                _REMOVED_COLUMN: remaining - remaining_after,
                _REMAINING_COLUMN: remaining_after,
            }
        )
        remaining = remaining_after
    return is_valid, pd.DataFrame(rows)


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
