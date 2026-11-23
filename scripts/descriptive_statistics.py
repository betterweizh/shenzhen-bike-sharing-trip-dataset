"""Describe the processed Shenzhen bike trips with summary statistics.

Every statistic is computed for two scopes: ``full`` (all processed
trips) and ``analysis_period`` (trips that start between
``ANALYSIS_START`` and ``ANALYSIS_END``, both inclusive). The period is
the stretch of steady, complete daily coverage found by the temporal
exploratory analysis.

Results go to ``output/descriptive_statistics/results``:

- ``summary_statistics.csv``: one row per scope, variable and statistic
  covering volume, trips per user, trip duration, straight-line trip
  distance and coordinate ranges;
- ``hourly_share.csv``: the share of trips by start hour;
- ``weekday_share.csv``: the share of trips by start weekday.

Figures for the analysis period go to
``output/descriptive_statistics/figures``: the duration and distance
distributions and the average hourly profile on weekdays and weekends.

Duration is ``END_TIME`` minus ``START_TIME`` in minutes. Distance is
the great-circle (haversine) distance between the start and end points
in kilometres, a lower bound on the distance ridden.

The coordinates must be WGS84: ``clean_up_raw_data.py`` records that in
the file's metadata, and the script stops with an error when the input
does not say WGS84 or the analysis period holds no trips.

The script takes no command-line arguments; edit the constants below
to change paths, the analysis period or figure settings.
"""

import logging
from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

logger = logging.getLogger(__name__)

TRIPS_PATH = Path('data/processed/bicycle_trips.parquet')
OUTPUT_DIR = Path('output/descriptive_statistics')
RESULTS_DIR = OUTPUT_DIR / 'results'
FIGURES_DIR = OUTPUT_DIR / 'figures'
LOG_PATH = OUTPUT_DIR / 'descriptive_statistics.log'
# Both dates are inclusive.
ANALYSIS_START = pd.Timestamp('2021-05-01')
ANALYSIS_END = pd.Timestamp('2021-08-28')
# Approximate bounding box of Shenzhen, in degrees.
SHENZHEN_MIN_LAT = 22.4
SHENZHEN_MAX_LAT = 22.9
SHENZHEN_MIN_LNG = 113.7
SHENZHEN_MAX_LNG = 114.7
# Trips longer than this are counted as unusually long.
LONG_TRIP_MINUTES = 180
QUANTILES = [
    0.01,
    0.05,
    0.25,
    0.50,
    0.75,
    0.95,
    0.99,
]
HISTOGRAM_BINS = 60
FIGURE_DPI = 200
# Width of the A4 text block (21 cm page minus 2.5 cm margins, about
# 6.3 in), so the figures print at 100 % and the fonts keep their size.
FIGURE_SIZE = (6.3, 3.2)
# Legends sit in one row above the plot; longer ones wrap to more rows.
LEGEND_MAX_COLUMNS = 8
# Start hours labelled on the hourly profile; every hour gets a minor
# tick.
HOUR_TICKS = list(range(0, 24, 3))
SERIES_COLOR = '#2a78d6'
SECOND_SERIES_COLOR = '#eb6834'
LINE_WIDTH = 2.0
MARKER_SIZE = 5.0
TEXT_COLOR = '#0b0b0b'
SECONDARY_TEXT_COLOR = '#52514e'
GRID_COLOR = '#e4e3df'
SURFACE_COLOR = '#fcfcfb'

_START_TIME_COLUMN = 'START_TIME'
_END_TIME_COLUMN = 'END_TIME'
_START_LAT_COLUMN = 'START_LAT'
_START_LNG_COLUMN = 'START_LNG'
_END_LAT_COLUMN = 'END_LAT'
_END_LNG_COLUMN = 'END_LNG'
_USER_KEY_COLUMN = 'USER_KEY'
_DURATION_COLUMN = 'DURATION_MIN'
_DISTANCE_COLUMN = 'DISTANCE_KM'
_COORDINATE_COLUMNS = [
    _START_LAT_COLUMN,
    _START_LNG_COLUMN,
    _END_LAT_COLUMN,
    _END_LNG_COLUMN,
]
_FULL_SCOPE = 'full'
_PERIOD_SCOPE = 'analysis_period'
_SCOPE_COLUMN = 'SCOPE'
_VARIABLE_COLUMN = 'VARIABLE'
_STATISTIC_COLUMN = 'STATISTIC'
_VALUE_COLUMN = 'VALUE'
_TRIPS_COLUMN = 'TRIPS'
_SHARE_COLUMN = 'SHARE'
_HOUR_COLUMN = 'HOUR'
_WEEKDAY_COLUMN = 'WEEKDAY'
_WEEKDAY_NAMES = [
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday',
    'Saturday',
    'Sunday',
]
# Saturday and Sunday in pandas' Monday-based weekday numbering.
_FIRST_WEEKEND_DAY = 5
_HOURS_PER_DAY = 24
# Metadata key written by clean_up_raw_data.py, read as DataFrame.attrs.
_COORDINATE_SYSTEM_ATTR = 'coordinate_system'
_EXPECTED_COORDINATE_SYSTEM = 'WGS84'
_EARTH_RADIUS_KM = 6371.0088
_SECONDS_PER_MINUTE = 60


def main() -> int:
    """Compute the descriptive statistics and write results and figures.

    Returns
    -------
    int
        Process exit code: 0 on success, 1 when the input is not marked
        as WGS84 or the analysis period holds no trips.
    """
    # The Agg backend renders to files and needs no display.
    mpl.use('Agg')
    _configure_logging(LOG_PATH)
    RESULTS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )
    FIGURES_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

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
    trips = _load_trips(TRIPS_PATH)
    # The trips are sorted by START_TIME, so the period is one slice.
    sorted_start_times = trips[_START_TIME_COLUMN]
    period_first = sorted_start_times.searchsorted(ANALYSIS_START)
    period_stop = sorted_start_times.searchsorted(
        ANALYSIS_END + pd.Timedelta(days=1)
    )
    scoped_trips = {
        _FULL_SCOPE: trips,
        _PERIOD_SCOPE: trips.iloc[period_first:period_stop],
    }
    logger.info(
        'analysis period %s to %s: %d trips',
        ANALYSIS_START.date(),
        ANALYSIS_END.date(),
        period_stop - period_first,
    )
    if period_stop == period_first:
        logger.error(
            'no trips between %s and %s in %s',
            ANALYSIS_START.date(),
            ANALYSIS_END.date(),
            TRIPS_PATH,
        )
        return 1

    summary_parts = []
    hourly_parts = []
    weekday_parts = []
    for scope, scope_trips in scoped_trips.items():
        summary_parts.append(_describe_scope(scope_trips, scope))
        start_times = scope_trips[_START_TIME_COLUMN]
        hourly_parts.append(
            _share_by(
                start_times.dt.hour,
                _HOUR_COLUMN,
                scope,
            )
        )
        weekday_parts.append(
            _share_by(
                start_times.dt.day_of_week,
                _WEEKDAY_COLUMN,
                scope,
            )
        )
    weekday_share = pd.concat(
        weekday_parts,
        ignore_index=True,
    )
    weekday_share[_WEEKDAY_COLUMN] = weekday_share[_WEEKDAY_COLUMN].map(
        dict(enumerate(_WEEKDAY_NAMES))
    )

    _write_csv(
        pd.concat(
            summary_parts,
            ignore_index=True,
        ),
        RESULTS_DIR / 'summary_statistics.csv',
    )
    _write_csv(
        pd.concat(
            hourly_parts,
            ignore_index=True,
        ),
        RESULTS_DIR / 'hourly_share.csv',
    )
    _write_csv(weekday_share, RESULTS_DIR / 'weekday_share.csv')

    period_trips = scoped_trips[_PERIOD_SCOPE]
    _save_figure(
        _plot_distribution(
            period_trips[_DURATION_COLUMN],
            title='Trip duration, analysis period',
            x_label='Duration (minutes)',
        ),
        FIGURES_DIR / 'duration_distribution.png',
    )
    _save_figure(
        _plot_distribution(
            period_trips[_DISTANCE_COLUMN],
            title='Straight-line trip distance, analysis period',
            x_label='Distance (km)',
        ),
        FIGURES_DIR / 'distance_distribution.png',
    )
    _save_figure(
        _plot_hourly_profile(period_trips[_START_TIME_COLUMN]),
        FIGURES_DIR / 'hourly_profile.png',
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


def _load_trips(trips_path: Path) -> pd.DataFrame:
    """Read the processed trips and add duration and distance.

    Parameters
    ----------
    trips_path : pathlib.Path
        Parquet file written by ``clean_up_raw_data.py``.

    Returns
    -------
    pandas.DataFrame
        The trips with two added float64 columns: ``DURATION_MIN``,
        the trip duration in minutes, and ``DISTANCE_KM``, the
        straight-line distance between start and end in kilometres.
    """
    logger.info('reading %s', trips_path)
    trips = pd.read_parquet(trips_path)
    logger.info('read %d trips', len(trips))
    duration = trips[_END_TIME_COLUMN] - trips[_START_TIME_COLUMN]
    trips[_DURATION_COLUMN] = duration.dt.total_seconds() / _SECONDS_PER_MINUTE
    trips[_DISTANCE_COLUMN] = _haversine_km(
        trips[_START_LAT_COLUMN].to_numpy(dtype=np.float64),
        trips[_START_LNG_COLUMN].to_numpy(dtype=np.float64),
        trips[_END_LAT_COLUMN].to_numpy(dtype=np.float64),
        trips[_END_LNG_COLUMN].to_numpy(dtype=np.float64),
    )
    return trips


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


def _describe_scope(trips: pd.DataFrame, scope: str) -> pd.DataFrame:
    """Compute every summary statistic for one scope of trips.

    Parameters
    ----------
    trips : pandas.DataFrame
        Trips with the columns added by ``_load_trips``; must not be
        empty.
    scope : str
        Scope name written to the ``SCOPE`` column.

    Returns
    -------
    pandas.DataFrame
        Columns ``SCOPE``, ``VARIABLE``, ``STATISTIC`` and ``VALUE``.
    """
    logger.info('describing scope %s', scope)
    # USER_KEY is a dense 0-based integer, so counting by position is
    # much faster than hashing; users without trips are dropped.
    user_trip_counts = np.bincount(trips[_USER_KEY_COLUMN].to_numpy())
    trips_per_user = pd.Series(user_trip_counts[user_trip_counts > 0])
    volume = {
        'trips': len(trips),
        'distinct_users': len(trips_per_user),
        'days_with_trips': trips[_START_TIME_COLUMN].dt.floor('D').nunique(),
        'first_start_time': trips[_START_TIME_COLUMN].min(),
        'last_start_time': trips[_START_TIME_COLUMN].max(),
    }
    durations = trips[_DURATION_COLUMN]
    distances = trips[_DISTANCE_COLUMN]
    duration_statistics = {
        **_distribution(durations),
        'share_non_positive': float((durations <= 0).mean()),
        f'share_over_{LONG_TRIP_MINUTES}_min': float(
            (durations > LONG_TRIP_MINUTES).mean()
        ),
    }
    distance_statistics = {
        **_distribution(distances),
        'share_zero': float((distances == 0).mean()),
    }
    rows = [
        *_statistic_rows('volume', volume),
        *_statistic_rows('trips_per_user', _distribution(trips_per_user)),
        *_statistic_rows('duration_min', duration_statistics),
        *_statistic_rows('distance_km', distance_statistics),
        *_coordinate_rows(trips),
    ]
    summary = pd.DataFrame(
        rows,
        columns=[
            _VARIABLE_COLUMN,
            _STATISTIC_COLUMN,
            _VALUE_COLUMN,
        ],
    )
    summary.insert(0, _SCOPE_COLUMN, scope)
    return summary


def _distribution(values: pd.Series) -> dict[str, float]:
    """Summarize a numeric variable with moments and quantiles.

    Parameters
    ----------
    values : pandas.Series of float
        Values to summarize; must not be empty.

    Returns
    -------
    dict of str to float
        Count, mean, standard deviation, minimum, the ``QUANTILES``
        (keyed ``p1``, ``p5`` and so on) and maximum.
    """
    summary = values.describe(percentiles=QUANTILES)
    # describe labels the quantiles '1%', '5%' and so on.
    return {
        f'p{label.removesuffix("%")}' if label.endswith('%') else label: float(value)
        for label, value in summary.items()
    }


def _coordinate_rows(trips: pd.DataFrame) -> list[tuple[str, str, object]]:
    """List the coordinate ranges and the share of trips inside Shenzhen.

    Parameters
    ----------
    trips : pandas.DataFrame
        Trips with start and end coordinates in degrees.

    Returns
    -------
    list of tuple of (str, str, object)
        ``(variable, statistic, value)`` rows: the minimum and maximum
        of each coordinate column, then the share of trips whose start
        and end both lie in the Shenzhen bounding box.
    """
    rows: list[tuple[str, str, object]] = []
    for column in _COORDINATE_COLUMNS:
        rows.append((column.lower(), 'min', float(trips[column].min())))
        rows.append((column.lower(), 'max', float(trips[column].max())))
    is_inside = (
        trips[_START_LAT_COLUMN].between(SHENZHEN_MIN_LAT, SHENZHEN_MAX_LAT)
        & trips[_START_LNG_COLUMN].between(SHENZHEN_MIN_LNG, SHENZHEN_MAX_LNG)
        & trips[_END_LAT_COLUMN].between(SHENZHEN_MIN_LAT, SHENZHEN_MAX_LAT)
        & trips[_END_LNG_COLUMN].between(SHENZHEN_MIN_LNG, SHENZHEN_MAX_LNG)
    )
    rows.append(('coordinates', 'share_inside_shenzhen', float(is_inside.mean())))
    return rows


def _statistic_rows(
    variable: str,
    statistics: dict[str, object],
) -> list[tuple[str, str, object]]:
    """Turn a statistic mapping into ``(variable, statistic, value)`` rows."""
    return [
        (variable, statistic, value)
        for statistic, value in statistics.items()
    ]


def _share_by(
    group_keys: pd.Series,
    key_column: str,
    scope: str,
) -> pd.DataFrame:
    """Count trips per group and the share of all trips in the scope.

    Parameters
    ----------
    group_keys : pandas.Series of int
        Group of each trip, such as its start hour.
    key_column : str
        Name of the group column in the result.
    scope : str
        Scope name written to the ``SCOPE`` column.

    Returns
    -------
    pandas.DataFrame
        Columns ``SCOPE``, the key column, ``TRIPS`` and ``SHARE``,
        sorted by key.
    """
    trip_counts = group_keys.value_counts().sort_index()
    shares = pd.DataFrame(
        {
            _SCOPE_COLUMN: scope,
            key_column: trip_counts.index,
            _TRIPS_COLUMN: trip_counts.to_numpy(),
            _SHARE_COLUMN: (trip_counts / trip_counts.sum()).to_numpy(),
        }
    )
    return shares


def _plot_distribution(
    values: pd.Series,
    *,
    title: str,
    x_label: str,
) -> Figure:
    """Plot a histogram of the values up to their 99th percentile.

    Parameters
    ----------
    values : pandas.Series of float
        Values to plot; values below 0 or above the 99th percentile
        are left out so the bulk of the distribution stays readable.
    title : str
        Figure title.
    x_label : str
        Label of the x axis.

    Returns
    -------
    matplotlib.figure.Figure
        The figure; the caller saves and closes it.
    """
    upper_limit = values.quantile(0.99)
    is_shown = values.between(0, upper_limit)
    figure, axes = _new_figure()
    axes.hist(
        values[is_shown],
        bins=HISTOGRAM_BINS,
        color=SERIES_COLOR,
        edgecolor=SURFACE_COLOR,
        linewidth=0.5,
    )
    axes.set_xlabel(
        x_label,
        color=SECONDARY_TEXT_COLOR,
    )
    _style_axes(
        axes,
        title=f'{title} (up to the 99th percentile)',
        y_label='Trips',
    )
    return figure


def _plot_hourly_profile(start_times: pd.Series) -> Figure:
    """Plot the average trips per start hour on weekdays and weekends.

    Parameters
    ----------
    start_times : pandas.Series of datetime64
        Trip start times; must include at least one weekday and one
        weekend day.

    Returns
    -------
    matplotlib.figure.Figure
        The figure; the caller saves and closes it.
    """
    start_dates = start_times.dt.floor('D')
    is_weekend = start_times.dt.day_of_week >= _FIRST_WEEKEND_DAY
    hours = start_times.dt.hour
    figure, axes = _new_figure()
    day_groups = [
        ('Weekdays', ~is_weekend, SERIES_COLOR),
        ('Weekends', is_weekend, SECOND_SERIES_COLOR),
    ]
    for label, is_in_group, color in day_groups:
        day_count = start_dates[is_in_group].nunique()
        hourly_trips = (
            hours[is_in_group]
            .value_counts()
            .reindex(
                range(_HOURS_PER_DAY),
                fill_value=0,
            )
        )
        axes.plot(
            hourly_trips.index,
            hourly_trips / day_count,
            color=color,
            linewidth=LINE_WIDTH,
            marker='o',
            markersize=MARKER_SIZE,
            label=f'{label} ({day_count} days)',
        )
    axes.set_xticks(HOUR_TICKS)
    axes.set_xticks(
        range(_HOURS_PER_DAY),
        minor=True,
    )
    axes.set_xlabel(
        'Start hour',
        color=SECONDARY_TEXT_COLOR,
    )
    _add_top_legend(
        figure,
        'Average trips per hour, analysis period',
    )
    _style_axes(
        axes,
        title=None,
        y_label='Trips per hour',
    )
    return figure


def _new_figure() -> tuple[Figure, Axes]:
    """Create an A4-width figure with one axes and constrained layout."""
    return plt.subplots(
        figsize=FIGURE_SIZE,
        layout='constrained',
    )


def _add_top_legend(figure: Figure, title: str) -> None:
    """Place the title and the legend in rows above the plot.

    The title becomes the legend's title, so it sits above the entries;
    a separate suptitle would overlap them, as the constrained layout
    stacks only one of the two. The layout shrinks the axes to make
    room, so the legend adds height instead of width.
    """
    handles, labels = figure.axes[0].get_legend_handles_labels()
    legend = figure.legend(
        handles,
        labels,
        loc='outside upper left',
        ncols=min(len(labels), LEGEND_MAX_COLUMNS),
        frameon=False,
        handlelength=1.5,
        columnspacing=1.0,
        labelcolor=SECONDARY_TEXT_COLOR,
        alignment='left',
        title=title,
        title_fontproperties={'size': 'large'},
    )
    legend.get_title().set_color(TEXT_COLOR)


def _style_axes(
    axes: Axes,
    *,
    title: str | None,
    y_label: str,
) -> None:
    """Apply the shared title, grid, number format and colors.

    Parameters
    ----------
    axes : matplotlib.axes.Axes
        Axes with counts on the y axis.
    title : str
        Figure title, written at the top left; None when
        ``_add_top_legend`` already put it on the legend.
    y_label : str
        Label of the y axis.
    """
    if title is not None:
        axes.figure.suptitle(
            title,
            x=0.0,
            ha='left',
            color=TEXT_COLOR,
            fontsize='large',
        )
    axes.set_ylabel(
        y_label,
        color=SECONDARY_TEXT_COLOR,
    )
    axes.set_ylim(bottom=0)
    axes.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _: f'{value:,.0f}'),
    )
    axes.grid(
        axis='y',
        color=GRID_COLOR,
        linewidth=0.8,
    )
    axes.set_axisbelow(True)
    axes.set_facecolor(SURFACE_COLOR)
    axes.tick_params(
        which='both',
        colors=SECONDARY_TEXT_COLOR,
    )
    for side in ('top', 'right'):
        axes.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        axes.spines[side].set_color(GRID_COLOR)


def _write_csv(table: pd.DataFrame, path: Path) -> None:
    """Write a result table as CSV without the index."""
    table.to_csv(
        path,
        index=False,
    )
    logger.info('wrote %d rows to %s', len(table), path)


def _save_figure(figure: Figure, path: Path) -> None:
    """Save a figure as PNG and release its memory."""
    figure.set_facecolor(SURFACE_COLOR)
    figure.savefig(
        path,
        dpi=FIGURE_DPI,
    )
    plt.close(figure)
    logger.info('saved %s', path)


if __name__ == '__main__':
    raise SystemExit(main())
