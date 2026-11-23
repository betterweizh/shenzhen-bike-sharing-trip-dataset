"""Explore the temporal coverage and volume of the Shenzhen bike trips.

The cleaned trips are read from the processed Parquet file, and only
``START_TIME`` is loaded. Each trip is counted on the calendar date it
starts. Days without any trip are kept as zero, so gaps in the scrape
stay visible.

Results go to ``output/exploratory_data_analysis/results``:

- ``daily_trips.csv``: the total trips per calendar date;
- ``monthly_average_daily_trips.csv``: per year-month, the days with
  trips, the total trips, and the average daily trips over the days
  with trips, so that a scrape gap does not lower the average;
- ``summary.csv``: overall counts and coverage.

Figures go to ``output/exploratory_data_analysis/figures``, all as
line-and-marker plots on a log scale, because the trip counts span
about six orders of magnitude:

- ``monthly_total_trips.png``: the total trips per year-month over the
  whole period;
- ``monthly_total_trips_by_year.png``: the total trips per calendar
  month, one line per year, to compare the seasonal pattern. The first
  and last months may be partial, as the data starts on 2017-04-03;
- ``daily_trips_by_month_<year>.png``: the total trips per day of
  month, one line per month of ``DAILY_BY_MONTH_YEAR``.

The script takes no command-line arguments; edit the constants below
to change paths or figure settings.
"""

import logging
from pathlib import Path

import matplotlib as mpl
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

logger = logging.getLogger(__name__)

TRIPS_PATH = Path('data/processed/bicycle_trips.parquet')
OUTPUT_DIR = Path('output/exploratory_data_analysis')
RESULTS_DIR = OUTPUT_DIR / 'results'
FIGURES_DIR = OUTPUT_DIR / 'figures'
LOG_PATH = OUTPUT_DIR / 'exploratory_data_analysis.log'
FIGURE_DPI = 200
# Width of the A4 text block (21 cm page minus 2.5 cm margins, about
# 6.3 in), so the figures print at 100 % and the fonts keep their size.
FIGURE_SIZE = (6.3, 3.2)
# Legends sit in one row above the plot; longer ones wrap to more rows.
LEGEND_MAX_COLUMNS = 8
# Days of month labelled on the daily-by-month plot; every day gets a
# minor tick.
DAY_OF_MONTH_TICKS = [1, 5, 10, 15, 20, 25, 30]
# Year whose daily trips are plotted per month; earlier years are too
# sparse for a daily view.
DAILY_BY_MONTH_YEAR = 2021
# Matplotlib colormap that colors the months of the daily-by-month plot.
DAILY_BY_MONTH_COLORMAP = 'coolwarm'
SERIES_COLOR = '#2a78d6'
# One color per year line, assigned in this fixed order.
LINE_COLORS = [
    '#2a78d6',
    '#eb6834',
    '#1baf7a',
    '#eda100',
    '#e87ba4',
    '#008300',
    '#6250d6',
    '#e34948',
]
LINE_WIDTH = 2.0
MARKER_SIZE = 5.0
TEXT_COLOR = '#0b0b0b'
SECONDARY_TEXT_COLOR = '#52514e'
GRID_COLOR = '#e4e3df'
SURFACE_COLOR = '#fcfcfb'

_TIME_COLUMN = 'START_TIME'
_DATE_COLUMN = 'DATE'
_MONTH_COLUMN = 'MONTH'
_TRIPS_COLUMN = 'TRIPS'
_DAYS_WITH_TRIPS_COLUMN = 'DAYS_WITH_TRIPS'
_TOTAL_TRIPS_COLUMN = 'TOTAL_TRIPS'
_AVERAGE_DAILY_TRIPS_COLUMN = 'AVERAGE_DAILY_TRIPS'
_DAILY_RESULT_NAME = 'daily_trips.csv'
_MONTHLY_RESULT_NAME = 'monthly_average_daily_trips.csv'
_SUMMARY_RESULT_NAME = 'summary.csv'
_MONTHLY_TOTAL_FIGURE_NAME = 'monthly_total_trips.png'
_MONTHLY_BY_YEAR_FIGURE_NAME = 'monthly_total_trips_by_year.png'
_DATE_FORMAT = '%Y-%m-%d'
_YEAR_FORMAT = '%Y'
_MAX_DAYS_IN_MONTH = 31
_MONTH_NAMES = [
    'Jan',
    'Feb',
    'Mar',
    'Apr',
    'May',
    'Jun',
    'Jul',
    'Aug',
    'Sep',
    'Oct',
    'Nov',
    'Dec',
]


def main() -> int:
    """Run the exploratory analysis and write its results and figures.

    Returns
    -------
    int
        Process exit code, 0 on success.
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

    start_times = _load_start_times(TRIPS_PATH)
    daily_trips = _count_daily_trips(start_times)
    monthly_trips = _average_daily_trips_by_month(daily_trips)
    summary = _summarize(daily_trips)

    _write_csv(daily_trips, RESULTS_DIR / _DAILY_RESULT_NAME)
    _write_csv(monthly_trips, RESULTS_DIR / _MONTHLY_RESULT_NAME)
    _write_csv(summary, RESULTS_DIR / _SUMMARY_RESULT_NAME)
    for metric, value in zip(
        summary['METRIC'],
        summary['VALUE'],
        strict=True,
    ):
        logger.info('%s: %s', metric, value)

    _save_figure(
        _plot_monthly_total_trips(monthly_trips),
        FIGURES_DIR / _MONTHLY_TOTAL_FIGURE_NAME,
    )
    _save_figure(
        _plot_monthly_total_trips_by_year(monthly_trips),
        FIGURES_DIR / _MONTHLY_BY_YEAR_FIGURE_NAME,
    )
    _save_figure(
        _plot_daily_trips_by_month(daily_trips, DAILY_BY_MONTH_YEAR),
        FIGURES_DIR / f'daily_trips_by_month_{DAILY_BY_MONTH_YEAR}.png',
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


def _load_start_times(trips_path: Path) -> pd.Series:
    """Read the trip start times from the processed trip file.

    Parameters
    ----------
    trips_path : pathlib.Path
        Parquet file written by ``clean_up_raw_data.py``.

    Returns
    -------
    pandas.Series
        Trip start times as naive local datetimes.
    """
    logger.info('reading %s from %s', _TIME_COLUMN, trips_path)
    trips = pd.read_parquet(
        trips_path,
        columns=[_TIME_COLUMN],
    )
    logger.info('read %d trips', len(trips))
    return trips[_TIME_COLUMN]


def _count_daily_trips(start_times: pd.Series) -> pd.DataFrame:
    """Count the trips that start on each calendar date.

    Parameters
    ----------
    start_times : pandas.Series
        Trip start times; must not be empty.

    Returns
    -------
    pandas.DataFrame
        Columns ``DATE`` and ``TRIPS``, one row per date from the first
        to the last trip date. Dates without trips have 0 trips.
    """
    trip_dates = start_times.dt.floor('D')
    trip_counts = trip_dates.value_counts().sort_index()
    all_dates = pd.date_range(
        trip_counts.index.min(),
        trip_counts.index.max(),
        freq='D',
    )
    trip_counts = trip_counts.reindex(
        all_dates,
        fill_value=0,
    )
    daily_trips = pd.DataFrame(
        {
            _DATE_COLUMN: all_dates,
            _TRIPS_COLUMN: trip_counts.to_numpy(),
        }
    )
    return daily_trips


def _average_daily_trips_by_month(daily_trips: pd.DataFrame) -> pd.DataFrame:
    """Average the daily trips within each year-month.

    Parameters
    ----------
    daily_trips : pandas.DataFrame
        Output of ``_count_daily_trips``.

    Returns
    -------
    pandas.DataFrame
        Columns ``MONTH`` (the first day of the month), ``DAYS_WITH_TRIPS``,
        ``TOTAL_TRIPS`` and ``AVERAGE_DAILY_TRIPS``. The average is taken
        over the days with trips and is 0 for a month without any.
    """
    months = daily_trips[_DATE_COLUMN].dt.to_period('M')
    has_trips = daily_trips[_TRIPS_COLUMN] > 0
    grouped_trips = daily_trips.groupby(months)
    monthly_trips = pd.DataFrame(
        {
            _DAYS_WITH_TRIPS_COLUMN: has_trips.groupby(months).sum(),
            _TOTAL_TRIPS_COLUMN: grouped_trips[_TRIPS_COLUMN].sum(),
        }
    )
    average_daily_trips = (
        monthly_trips[_TOTAL_TRIPS_COLUMN]
        / monthly_trips[_DAYS_WITH_TRIPS_COLUMN]
    )
    monthly_trips[_AVERAGE_DAILY_TRIPS_COLUMN] = average_daily_trips.fillna(0.0)
    monthly_trips.index = monthly_trips.index.to_timestamp()
    monthly_trips.index.name = _MONTH_COLUMN
    return monthly_trips.reset_index()


def _summarize(daily_trips: pd.DataFrame) -> pd.DataFrame:
    """Summarize the trip volume and the date coverage.

    Parameters
    ----------
    daily_trips : pandas.DataFrame
        Output of ``_count_daily_trips``.

    Returns
    -------
    pandas.DataFrame
        Columns ``METRIC`` and ``VALUE``, one row per summary metric.
    """
    trip_counts = daily_trips[_TRIPS_COLUMN]
    dates = daily_trips[_DATE_COLUMN]
    days_with_trips = int((trip_counts > 0).sum())
    busiest_index = trip_counts.idxmax()
    metrics = {
        'total_trips': int(trip_counts.sum()),
        'first_date': dates.iloc[0].strftime(_DATE_FORMAT),
        'last_date': dates.iloc[-1].strftime(_DATE_FORMAT),
        'calendar_days': len(daily_trips),
        'days_with_trips': days_with_trips,
        'days_without_trips': len(daily_trips) - days_with_trips,
        'mean_daily_trips_on_days_with_trips': round(
            trip_counts.sum() / days_with_trips,
            1,
        ),
        'busiest_date': dates[busiest_index].strftime(_DATE_FORMAT),
        'busiest_date_trips': int(trip_counts[busiest_index]),
    }
    return pd.DataFrame(
        {
            'METRIC': list(metrics),
            'VALUE': list(metrics.values()),
        }
    )


def _plot_monthly_total_trips(monthly_trips: pd.DataFrame) -> Figure:
    """Plot the total trips per year-month as a line with markers.

    Parameters
    ----------
    monthly_trips : pandas.DataFrame
        Output of ``_average_daily_trips_by_month``.

    Returns
    -------
    matplotlib.figure.Figure
        The figure; the caller saves and closes it.
    """
    figure, axes = _new_figure()
    axes.plot(
        monthly_trips[_MONTH_COLUMN],
        _positive_or_missing(monthly_trips[_TOTAL_TRIPS_COLUMN]),
        color=SERIES_COLOR,
        linewidth=LINE_WIDTH,
        marker='o',
        markersize=MARKER_SIZE,
    )
    axes.xaxis.set_major_locator(mdates.YearLocator())
    axes.xaxis.set_minor_locator(mdates.MonthLocator())
    axes.xaxis.set_major_formatter(mdates.DateFormatter(_YEAR_FORMAT))
    _style_axes(
        axes,
        title='Monthly total trips',
        y_label='Trips per month (log scale)',
    )
    return figure


def _plot_monthly_total_trips_by_year(monthly_trips: pd.DataFrame) -> Figure:
    """Plot the total trips per calendar month, one line per year.

    Parameters
    ----------
    monthly_trips : pandas.DataFrame
        Output of ``_average_daily_trips_by_month``.

    Returns
    -------
    matplotlib.figure.Figure
        The figure; the caller saves and closes it.

    Raises
    ------
    ValueError
        If the data spans more years than ``LINE_COLORS`` has colors.
    """
    years = monthly_trips[_MONTH_COLUMN].dt.year
    month_numbers = monthly_trips[_MONTH_COLUMN].dt.month
    distinct_years = sorted(years.unique())
    if len(distinct_years) > len(LINE_COLORS):
        raise ValueError(f'more years than colors: {distinct_years!r}')

    figure, axes = _new_figure()
    for year, color in zip(
        distinct_years,
        LINE_COLORS[:len(distinct_years)],
        strict=True,
    ):
        is_year = years == year
        year_months = month_numbers[is_year]
        year_trips = _positive_or_missing(
            monthly_trips.loc[is_year, _TOTAL_TRIPS_COLUMN],
        )
        axes.plot(
            year_months,
            year_trips,
            color=color,
            linewidth=LINE_WIDTH,
            marker='o',
            markersize=MARKER_SIZE,
            label=str(year),
        )
    axes.set_xticks(
        range(1, len(_MONTH_NAMES) + 1),
        labels=_MONTH_NAMES,
    )
    _add_top_legend(
        figure,
        'Monthly total trips by year',
    )
    _style_axes(
        axes,
        title=None,
        y_label='Trips per month (log scale)',
    )
    return figure


def _plot_daily_trips_by_month(
    daily_trips: pd.DataFrame,
    year: int,
) -> Figure:
    """Plot the total trips per day of month, one line per month.

    Parameters
    ----------
    daily_trips : pandas.DataFrame
        Output of ``_count_daily_trips``.
    year : int
        Only the months of this year are plotted; it must have data.

    Returns
    -------
    matplotlib.figure.Figure
        The figure; the caller saves and closes it.

    Notes
    -----
    Line colors are sampled evenly from ``DAILY_BY_MONTH_COLORMAP``, from
    the first month with data (cool) to the last (warm).
    """
    dates = daily_trips[_DATE_COLUMN]
    is_in_year = dates.dt.year == year
    year_dates = dates[is_in_year]
    year_trips = daily_trips.loc[is_in_year, _TRIPS_COLUMN]
    month_numbers = year_dates.dt.month
    distinct_months = sorted(month_numbers.unique())
    colormap = mpl.colormaps[DAILY_BY_MONTH_COLORMAP]
    month_colors = colormap(np.linspace(0.0, 1.0, len(distinct_months)))

    figure, axes = _new_figure()
    for month_number, color in zip(
        distinct_months,
        month_colors,
        strict=True,
    ):
        is_month = month_numbers == month_number
        axes.plot(
            year_dates[is_month].dt.day,
            _positive_or_missing(year_trips[is_month]),
            color=color,
            linewidth=LINE_WIDTH,
            marker='o',
            markersize=MARKER_SIZE,
            label=_MONTH_NAMES[month_number - 1],
        )
    axes.set_xticks(DAY_OF_MONTH_TICKS)
    axes.set_xticks(
        range(1, _MAX_DAYS_IN_MONTH + 1),
        minor=True,
    )
    axes.set_xlabel(
        'Day of month',
        color=SECONDARY_TEXT_COLOR,
    )
    _add_top_legend(
        figure,
        f'Daily total trips by month, {year}',
    )
    _style_axes(
        axes,
        title=None,
        y_label='Trips per day (log scale)',
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


def _positive_or_missing(trip_counts: pd.Series) -> pd.Series:
    """Replace zero counts with NaN, which a log axis cannot show."""
    return trip_counts.where(trip_counts > 0)


def _style_axes(
    axes: Axes,
    *,
    title: str | None,
    y_label: str,
) -> None:
    """Apply the shared title, log y axis, grid and number format.

    Parameters
    ----------
    axes : matplotlib.axes.Axes
        Axes with trip counts on the y axis.
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
    axes.set_yscale('log')
    axes.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _: f'{value:,.0f}'),
    )
    axes.grid(
        axis='y',
        which='major',
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
