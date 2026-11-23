# Shenzhen dockless bike-sharing trip dataset

This record contains the trips from Shenzhen's dockless bike-sharing systems, published by the Shenzhen Municipal Government Data Open Platform (深圳市政府数据开放平台, opendata.sz.gov.cn). This dataset includes:

- **Raw data:** the 244,673,004 trip records exactly as downloaded from the platform's API, as five zip archives of CSV files.
- **Processed data:** 243,951,347 cleaned trips from 10,768,628 masked users, from 2017-04-03 to 2021-08-31 (99.91 % of them in 2021), with coordinates converted to WGS84, as two Parquet files.

The code that turns the raw data into the processed data, the exploratory and descriptive analyses, the study-period screening, and the detailed explanations are in the GitHub repository: [shenzhen-bike-sharing-trip-dataset](https://github.com/betterweizh/shenzhen-bike-sharing-trip-dataset). This record holds the data files only.

## 1. Files and checksums

| File                           | Size    | MD5                                |
|:------------------------------ |:------- |:---------------------------------- |
| `bicycle_trip_0_4999.zip`      | 1.81 GB | `679da2b4a0cd5b1d3c2fbbcacbb93c5e` |
| `bicycle_trip_5000_9999.zip`   | 1.81 GB | `d72ba420c9001c42b11248a12bfba4ce` |
| `bicycle_trip_10000_14999.zip` | 1.82 GB | `08e01cc13f4b44b66feef6d9dd843f46` |
| `bicycle_trip_15000_19999.zip` | 1.79 GB | `86ef33d9bf14b02e6af75ecc00801b94` |
| `bicycle_trip_20000_24999.zip` | 1.63 GB | `1f3dde4fc1a787439cc9a5172501a78d` |
| `bicycle_trips.parquet`        | 3.80 GB | `0447dd37cf038d49d01aeb1a5817f411` |
| `bicycle_users.parquet`        | 149 MB  | `46b5b4789b8cc5cad3309212d8f077ea` |

To rerun the processing code, put the five zip archives in `data/raw/` and the two Parquet files in `data/processed/` inside the GitHub project.

```
data/
  raw/                               raw archives
    bicycle_trip_0_4999.zip          API pages 0-4999 (6 trip CSVs)
    bicycle_trip_5000_9999.zip       API pages 5000-9999 (5 trip CSVs)
    bicycle_trip_10000_14999.zip     API pages 10000-14999 (5 trip CSVs)
    bicycle_trip_15000_19999.zip     API pages 15000-19999 (5 trip CSVs)
    bicycle_trip_20000_24999.zip     API pages 20000-24464 (5 trip CSVs)
  processed/                         written by scripts/clean_up_raw_data.py
    bicycle_trips.parquet            243,951,347 cleaned trips
    bicycle_users.parquet            USER_KEY to masked USER_ID mapping
```

## 2. Raw data

### (1) Archives

Each archive `bicycle_trip_<a>_<b>.zip` contains one folder of the same name holding trip files `bicycle_trip_<x>_<y>.csv`. Together they hold 26 CSV files, 31.27 GB uncompressed.

| Archive                        | API pages   | Trip CSVs | Uncompressed |
|:------------------------------ |:----------- |:--------- |:------------ |
| `bicycle_trip_0_4999.zip`      | 0–4999      | 6         | 6.48 GB      |
| `bicycle_trip_5000_9999.zip`   | 5000–9999   | 5         | 6.38 GB      |
| `bicycle_trip_10000_14999.zip` | 10000–14999 | 5         | 6.38 GB      |
| `bicycle_trip_15000_19999.zip` | 15000–19999 | 5         | 6.36 GB      |
| `bicycle_trip_20000_24999.zip` | 20000–24464 | 5         | 5.68 GB      |

The data were downloaded page by page from the platform's API.

- **Pages.** Each page holds about 10,000 rows, so a 1,000-page file has 10,001,000 rows.
- **File names.** The numbers `<x>_<y>` are API page indices, not row counts or dates.
- **Inconsistent names.** Some names in the last archive have inconsistent ranges (for example `bicycle_trip_21000_22999.csv`), so coverage should not be inferred from file names alone.

### (2) Fields

Each CSV has a header row and these fields:

| Field        | Raw format                 | Description                                                                            |
|:------------ |:-------------------------- |:-------------------------------------------------------------------------------------- |
| `START_TIME` | `YYYY-MM-DD HH:MM:SS[.0]`  | Trip start time, local time                                                            |
| `START_LAT`  | decimal degrees            | Start latitude, GCJ-02                                                                 |
| `START_LNG`  | decimal degrees            | Start longitude, GCJ-02                                                                |
| `END_TIME`   | `YYYY-MM-DD HH:MM:SS[.0]`  | Trip end time                                                                          |
| `END_LAT`    | decimal degrees            | End latitude, GCJ-02                                                                   |
| `END_LNG`    | decimal degrees            | End longitude, GCJ-02                                                                  |
| `COM_ID`     | `0755**` or empty          | Masked company code; only in `bicycle_trip_0_999.csv` and `bicycle_trip_1000_1435.csv` |
| `USER_ID`    | 22 hex characters + 10 `*` | Masked user identifier                                                                 |

- **Times.** Most files write times with a `.0` fraction; the earliest rows have none. The fraction is always zero.
- **Coordinates.** The number of decimals varies between rows.
- **Coverage.** Records start on 2017-04-03, but coverage before 2021 is sparse and has gaps. Daily coverage is more plausible in year 2021 (see [`docs/data_preprocessing.md`](https://github.com/betterweizh/shenzhen-bike-sharing-trip-dataset/blob/HEAD/docs/data_preprocessing.md) in the GitHub repository).

### (3) Coordinate system

The platform documents the coordinates as BD-09. A month-by-month test of trip start and end points against the OpenStreetMap road network shows that they are in fact **GCJ-02**. In Shenzhen, GCJ-02 points lie about 600 m from their true (WGS84) position. The raw files are left unchanged, so they are in GCJ-02. The test is described in [`docs/crs_detection.md`](https://github.com/betterweizh/shenzhen-bike-sharing-trip-dataset/blob/HEAD/docs/crs_detection.md) of the GitHub repository.

## 3. Processed data

The processed files were produced from the raw archives by [`scripts/clean_up_raw_data.py`](https://github.com/betterweizh/shenzhen-bike-sharing-trip-dataset/blob/HEAD/scripts/clean_up_raw_data.py) in the GitHub repository. Each raw row was:

1. **checked:** rows were rejected when they had an invalid date-time, a coordinate outside the global range, or a malformed `USER_ID`;
2. **converted** from GCJ-02 to WGS84, with an inversion error below 1 cm. The converted coordinates are stored as float32 to reduce file size, which adds a rounding error below 1 m;
3. **deduplicated:** rows identical in every field were removed;
4. **sorted** by `START_TIME`.

| Step                             | Rows        |
|:-------------------------------- |:----------- |
| Rows read from the raw CSVs      | 244,673,004 |
| Rejected by the checks           | 15          |
| Rejected by the WGS84 conversion | 0           |
| Exact duplicates removed         | 721,642     |
| Trips in `bicycle_trips.parquet` | 243,951,347 |

The full processing steps are described in [`docs/data_preprocessing.md`](https://github.com/betterweizh/shenzhen-bike-sharing-trip-dataset/blob/HEAD/docs/data_preprocessing.md) of the GitHub repository.

### (1) `bicycle_trips.parquet`

One row per trip, sorted by `START_TIME`.

| Column       | Type          | Unit            | Description                                          |
|:------------ |:------------- |:--------------- |:---------------------------------------------------- |
| `START_TIME` | timestamp (s) | local time      | Trip start, whole seconds                            |
| `START_LAT`  | float32       | degrees (WGS84) | Start latitude                                       |
| `START_LNG`  | float32       | degrees (WGS84) | Start longitude                                      |
| `END_TIME`   | timestamp (s) | local time      | Trip end                                             |
| `END_LAT`    | float32       | degrees (WGS84) | End latitude                                         |
| `END_LNG`    | float32       | degrees (WGS84) | End longitude                                        |
| `USER_KEY`   | int32         | –               | Anonymous user key; joins to `bicycle_users.parquet` |

`COM_ID` is dropped because it carries no information. The file's pandas metadata (`DataFrame.attrs`) records `coordinate_system = 'WGS84'`.

### (2) `bicycle_users.parquet`

One row for each of the 10,768,628 users.

| Column     | Type   | Description                                                                                |
|:---------- |:------ |:------------------------------------------------------------------------------------------ |
| `USER_KEY` | int32  | 0-based key, assigned in sorted `USER_ID` order                                            |
| `USER_ID`  | string | The 22-character hexadecimal part of the provider's masked `USER_ID`, without the `*` mask |

### (3) Notes for users

- **Screening.** The processed trips still include implausible records, such as negative or very long durations, very short or long distances, and points outside Shenzhen. The script [`scripts/selected_period_trips.py`](https://github.com/betterweizh/shenzhen-bike-sharing-trip-dataset/blob/HEAD/scripts/selected_period_trips.py) in the GitHub repository screens them out for the study period (2021-05-01 to 2021-08-28) on duration, distance, the Shenzhen boundary and speed. Users who need clean trips should apply similar screening.
- **Reading.** The trip file is large in memory (about 244 million rows). Read only the columns and rows you need, for example with `pandas.read_parquet(path, columns=[...], filters=[...])` or `pyarrow.dataset`.