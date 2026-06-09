# Data Warehouse ER Diagram

This Mermaid ER diagram reflects the current warehouse tables defined in [app/models.py](/Users/likitpreeyanon/projects/health-dynamics/app/models.py) and present in `health_dynamics.db`.

```mermaid
erDiagram
    DIM_PERSON {
        int person_key PK
        string cms_code UK
        string employee_id UK
        string full_name
        string gender
        string department
    }

    DIM_DATE {
        int date_key PK
        int checkup_year
        date checkup_date UK
    }

    DIM_MEASURE {
        int measure_key PK
        string measure_code UK
        string measure_name
        string category
        string unit
    }

    FACT_CHECKUP_MEASUREMENT {
        int fact_key PK
        int person_key FK
        int date_key FK
        int measure_key FK
        float value_numeric
        string raw_value
        string source_file
    }

    FACT_PERSON_CHECKUP_SNAPSHOT {
        int snapshot_key PK
        int person_key FK
        int date_key FK
        int age
        float weight
        float height
        float bmi
        float systolic_bp
        float diastolic_bp
    }

    FACT_HEALTH_TRAJECTORY {
        int trajectory_key PK
        int person_key FK
        int measure_key FK
        int from_date_key FK
        int to_date_key FK
        float previous_value
        float current_value
        float delta_value
        float percent_change
        string status_transition
        string trajectory_class
        string risk_direction
    }

    FACT_HEALTH_ARCHETYPE {
        int archetype_key PK
        int person_key FK
        int measure_key FK
        int first_date_key FK
        int last_date_key FK
        int num_checkups
        string archetype_name
        float confidence_score
    }

    FACT_POPULATION_FORECAST {
        int forecast_key PK
        int measure_key FK
        int base_date_key FK
        int forecast_horizon_years
        int current_normal_count
        int current_borderline_count
        int current_abnormal_count
        float forecast_normal_count
        float forecast_borderline_count
        float forecast_abnormal_count
    }

    FACT_DATA_QUALITY {
        int quality_key PK
        string check_type
        string target_table
        string target_field
        int measure_key FK
        int total_records
        int missing_count
        int invalid_count
        string warning_level
    }

    DIM_PERSON ||--o{ FACT_CHECKUP_MEASUREMENT : has
    DIM_DATE ||--o{ FACT_CHECKUP_MEASUREMENT : recorded_on
    DIM_MEASURE ||--o{ FACT_CHECKUP_MEASUREMENT : measures

    DIM_PERSON ||--o{ FACT_PERSON_CHECKUP_SNAPSHOT : has
    DIM_DATE ||--o{ FACT_PERSON_CHECKUP_SNAPSHOT : captured_on

    DIM_PERSON ||--o{ FACT_HEALTH_TRAJECTORY : has
    DIM_MEASURE ||--o{ FACT_HEALTH_TRAJECTORY : tracks
    DIM_DATE ||--o{ FACT_HEALTH_TRAJECTORY : from_date
    DIM_DATE ||--o{ FACT_HEALTH_TRAJECTORY : to_date

    DIM_PERSON ||--o{ FACT_HEALTH_ARCHETYPE : assigned_to
    DIM_MEASURE ||--o{ FACT_HEALTH_ARCHETYPE : classified_for
    DIM_DATE ||--o{ FACT_HEALTH_ARCHETYPE : first_seen
    DIM_DATE ||--o{ FACT_HEALTH_ARCHETYPE : last_seen

    DIM_MEASURE ||--o{ FACT_POPULATION_FORECAST : forecast_for
    DIM_DATE ||--o{ FACT_POPULATION_FORECAST : based_on

    DIM_MEASURE |o--o{ FACT_DATA_QUALITY : optionally_scoped_to
```

## Notes

- `fact_checkup_measurement` is the central granular fact table.
- `fact_person_checkup_snapshot` is a denormalized per-person, per-checkup summary.
- `fact_health_trajectory` and `fact_health_archetype` are derived analytical facts built from repeated measurements over time.
- `fact_population_forecast` is aggregated at the measure and base-date level, not the person level.
- `fact_data_quality` is an audit fact table and only links to `dim_measure` when a check is measure-specific.
- `executive_brief` and `exploration_brief` exist in the same database but are application output tables, not part of the warehouse star schema.
