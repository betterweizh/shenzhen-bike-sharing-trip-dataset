# Shenzhen dockless bike-sharing trips: preprocessing and exploration

Code and documentation for turning the Shenzhen dockless bike-sharing trip records into a clean, analysis-ready trip set. The records were published by the Shenzhen Municipal Government Data Open Platform (深圳市政府数据开放平台, opendata.sz.gov.cn). The pipeline:

- validates, deduplicates and merges about 245 million raw trip records;
- converts their coordinates from GCJ-02 to WGS84;
- explores their temporal coverage and describes the trips statistically;
- extracts and screens the trips of the study period.

## Data

The data are published separately on Zenodo: [Shenzhen dockless bike-sharing trip dataset](https://doi.org/10.5281/zenodo.24440955). The record holds the five raw zip archives as downloaded from the platform and the two processed Parquet files, with an MD5 checksum for each file. Data descriptions are in [zenodo_dataset_description.md](/zenodo_dataset_description.md).

This repository holds the code and documentation only.

## Key facts

| Item                 | Value                                                             |
| -------------------- | ----------------------------------------------------------------- |
| Raw trip records     | 244,673,004 (2017-04-03 to 2021-08-31)                            |
| Trips after cleaning | 243,951,347 from 10,768,628 masked users                          |
| Coordinate system    | Raw data GCJ-02 (labelled BD-09 by the source); all outputs WGS84 |

**Coordinate system.** The data platform states that the coordinates are BD-09. A test against the OpenStreetMap road network shows that they are GCJ-02, so they are converted from GCJ-02 to WGS84 during cleaning ([docs/crs_detection.md](docs/crs_detection.md)).

## Project structure

```
# Data (download from Zenodo)
data/
  raw/                               raw archives from Zenodo; place them here to rerun
    bicycle_trip_0_4999.zip          API pages 0-4999 (6 trip CSVs)
    bicycle_trip_5000_9999.zip       API pages 5000-9999 (5 trip CSVs)
    bicycle_trip_10000_14999.zip     API pages 10000-14999 (5 trip CSVs)
    bicycle_trip_15000_19999.zip     API pages 15000-19999 (5 trip CSVs)
    bicycle_trip_20000_24999.zip     API pages 20000-24464 (5 trip CSVs)
  processed/                         written by scripts/clean_up_raw_data.py
    bicycle_trips.parquet            243,951,347 cleaned trips in WGS84, sorted by start time
    bicycle_users.parquet            USER_KEY to masked USER_ID mapping
    bicycle_trips_rejected.csv       the 15 raw rows rejected by validation, with reasons (rerun only)
    clean_up_raw_data.log            cleaning log (rerun only)

# Scripts
scripts/                             pipeline steps, run in this order
  clean_up_raw_data.py               raw zip archives -> cleaned WGS84 trips
  exploratory_data_analysis.py       daily and monthly coverage; selects the study period
  descriptive_statistics.py          volume, users, duration, distance, time-of-day statistics
  selected_period_trips.py           study-period extract with trip screening

# Results (created by running the scripts)
output/
  exploratory_data_analysis/         from scripts/exploratory_data_analysis.py
  descriptive_statistics/            from scripts/descriptive_statistics.py
  selected_trips/                    from scripts/selected_period_trips.py

# Documents
README.md                            this file
zenodo_dataset_description.md        description of the Zenodo data record
docs/
  data_preprocessing.md              data source, raw format, cleaning, temporal exploration, screening
  crs_detection.md                   how the raw coordinate system was identified
  figures/                           figures used by the two documents

# Project files
src/shenzhen_bike_sharing/           package skeleton (no library code yet)
pyproject.toml                       project metadata and dependencies
uv.lock                              exact dependency versions for uv
.python-version                      Python version used by uv (3.12)
LICENSE                              CC BY-NC 4.0
```

The `data/` and `output/` folders are not tracked in this repository because of their size. To rerun the pipeline, download the raw archives from the [Zenodo record](https://doi.org/10.5281/zenodo.24440955) into `data/raw/`. To skip the hour-long cleaning step, also put the two Parquet files in `data/processed/` and start with `exploratory_data_analysis.py`. [docs/data_preprocessing.md](docs/data_preprocessing.md) describes the data format and every processing step.

## Requirements and usage

The project needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                                             # create the environment from uv.lock
uv run python scripts/clean_up_raw_data.py          # about 60 minutes
uv run python scripts/exploratory_data_analysis.py  # under 1 minute
uv run python scripts/descriptive_statistics.py     # about 4 minutes
uv run python scripts/selected_period_trips.py      # about 2 minutes
```

Run the scripts from the repository root, in this order; each one reads the output of an earlier step. They take no command-line arguments: paths, the study period, the screening thresholds and figure settings are constants at the top of each script.

## Citation

If you use the data or code, please cite the Zenodo dataset and the related article:

> Zhou, W. (2026). *Shenzhen dockless bike-sharing trip dataset* (Version 1.0.0) [Data set]. Zenodo. https://doi.org/10.5281/zenodo.24440955
> 
> Zhou, W., & Ren, J. (2027). Comparative analysis of linear regression, spatial models, and machine learning models in exploring the relationship between the built environment and bike-sharing demand. *Journal of Urban Planning and Development*, 153(1), 05026051. https://doi.org/10.1061/JUPDDM.UPENG-6762

```bibtex
@dataset{zhou_2026_shenzhen_bike_sharing,
  author    = {Zhou, Wei},
  title     = {Shenzhen dockless bike-sharing trip dataset},
  year      = {2026},
  version   = {1.0.0},
  publisher = {Zenodo},
  doi       = {10.5281/zenodo.24440955},
  url       = {https://doi.org/10.5281/zenodo.24440955}
}

@article{zhou_2027_built_environment,
  author    = {Zhou, Wei and Ren, Jingxuan},
  title     = {Comparative analysis of linear regression, spatial models, and machine learning models in exploring the relationship between the built environment and bike-sharing demand},
  journal   = {Journal of Urban Planning and Development},
  year      = {2027},
  volume    = {153},
  number    = {1},
  pages     = {05026051},
  publisher = {American Society of Civil Engineers},
  doi       = {10.1061/JUPDDM.UPENG-6762}
}
```

Please also acknowledge the Shenzhen Municipal Government Data Open Platform as the source of the trip data.

## Acknowledgement

This project was developed with the assistance of Claude Opus 5.5.

## License

The processed data and this documentation are released under [Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/). The raw trip records remain subject to the terms of the Shenzhen Municipal Government Data Open Platform.
