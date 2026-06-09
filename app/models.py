from app import db
from datetime import datetime, UTC


class DimPerson(db.Model):
    __tablename__ = "dim_person"

    person_key = db.Column(db.Integer, primary_key=True)
    cms_code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    employee_id = db.Column(db.String(50), unique=True, nullable=True, index=True)
    full_name = db.Column(db.String(255), nullable=False)
    prefix = db.Column(db.String(50), nullable=True)
    gender = db.Column(db.String(20), nullable=True)
    department = db.Column(db.String(100), nullable=True)

    measurements = db.relationship(
        "FactCheckupMeasurement",
        back_populates="person",
        lazy="dynamic",
    )
    snapshots = db.relationship(
        "FactPersonCheckupSnapshot",
        back_populates="person",
        lazy="dynamic",
    )
    trajectories = db.relationship(
        "FactHealthTrajectory",
        back_populates="person",
        lazy="dynamic",
    )
    archetypes = db.relationship(
        "FactHealthArchetype",
        back_populates="person",
        lazy="dynamic",
    )


class DimDate(db.Model):
    __tablename__ = "dim_date"

    date_key = db.Column(db.Integer, primary_key=True)
    checkup_year = db.Column(db.Integer, nullable=False, index=True)
    checkup_date = db.Column(db.Date, nullable=False, unique=True, index=True)

    measurements = db.relationship(
        "FactCheckupMeasurement",
        back_populates="date",
        lazy="dynamic",
    )
    snapshots = db.relationship(
        "FactPersonCheckupSnapshot",
        back_populates="date",
        lazy="dynamic",
    )
    trajectories_from = db.relationship(
        "FactHealthTrajectory",
        back_populates="from_date",
        lazy="dynamic",
        foreign_keys="FactHealthTrajectory.from_date_key",
    )
    trajectories_to = db.relationship(
        "FactHealthTrajectory",
        back_populates="to_date",
        lazy="dynamic",
        foreign_keys="FactHealthTrajectory.to_date_key",
    )
    archetypes_first = db.relationship(
        "FactHealthArchetype",
        back_populates="first_date",
        lazy="dynamic",
        foreign_keys="FactHealthArchetype.first_date_key",
    )
    archetypes_last = db.relationship(
        "FactHealthArchetype",
        back_populates="last_date",
        lazy="dynamic",
        foreign_keys="FactHealthArchetype.last_date_key",
    )
    population_forecasts = db.relationship(
        "FactPopulationForecast",
        back_populates="base_date",
        lazy="dynamic",
    )


class DimMeasure(db.Model):
    __tablename__ = "dim_measure"

    measure_key = db.Column(db.Integer, primary_key=True)
    measure_code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    measure_name = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(100), nullable=True, index=True)
    unit = db.Column(db.String(50), nullable=True)

    measurements = db.relationship(
        "FactCheckupMeasurement",
        back_populates="measure",
        lazy="dynamic",
    )
    trajectories = db.relationship(
        "FactHealthTrajectory",
        back_populates="measure",
        lazy="dynamic",
    )
    archetypes = db.relationship(
        "FactHealthArchetype",
        back_populates="measure",
        lazy="dynamic",
    )
    population_forecasts = db.relationship(
        "FactPopulationForecast",
        back_populates="measure",
        lazy="dynamic",
    )
    data_quality_checks = db.relationship(
        "FactDataQuality",
        back_populates="measure",
        lazy="dynamic",
    )


class FactCheckupMeasurement(db.Model):
    __tablename__ = "fact_checkup_measurement"

    fact_key = db.Column(db.Integer, primary_key=True)
    person_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_person.person_key"),
        nullable=False,
        index=True,
    )
    date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    measure_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_measure.measure_key"),
        nullable=False,
        index=True,
    )
    value_numeric = db.Column(db.Float, nullable=True)
    raw_value = db.Column(db.String(255), nullable=True)
    source_file = db.Column(db.String(255), nullable=True)

    person = db.relationship("DimPerson", back_populates="measurements")
    date = db.relationship("DimDate", back_populates="measurements")
    measure = db.relationship("DimMeasure", back_populates="measurements")


class FactPersonCheckupSnapshot(db.Model):
    __tablename__ = "fact_person_checkup_snapshot"

    snapshot_key = db.Column(db.Integer, primary_key=True)
    person_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_person.person_key"),
        nullable=False,
        index=True,
    )
    date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    age = db.Column(db.Integer, nullable=True)
    weight = db.Column(db.Float, nullable=True)
    height = db.Column(db.Float, nullable=True)
    bmi = db.Column(db.Float, nullable=True)
    systolic_bp = db.Column(db.Float, nullable=True)
    diastolic_bp = db.Column(db.Float, nullable=True)
    source_file = db.Column(db.String(255), nullable=True)

    person = db.relationship("DimPerson", back_populates="snapshots")
    date = db.relationship("DimDate", back_populates="snapshots")


class FactHealthTrajectory(db.Model):
    __tablename__ = "fact_health_trajectory"

    trajectory_key = db.Column(db.Integer, primary_key=True)
    person_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_person.person_key"),
        nullable=False,
        index=True,
    )
    measure_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_measure.measure_key"),
        nullable=False,
        index=True,
    )
    from_date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    to_date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    previous_value = db.Column(db.Float, nullable=True)
    current_value = db.Column(db.Float, nullable=True)
    delta_value = db.Column(db.Float, nullable=True)
    percent_change = db.Column(db.Float, nullable=True)
    previous_status = db.Column(db.String(32), nullable=False, default="unknown")
    current_status = db.Column(db.String(32), nullable=False, default="unknown")
    status_transition = db.Column(db.String(64), nullable=False)
    trajectory_class = db.Column(db.String(32), nullable=False)
    risk_direction = db.Column(db.String(32), nullable=False)
    interpretation = db.Column(db.String(255), nullable=True)

    person = db.relationship("DimPerson", back_populates="trajectories")
    measure = db.relationship("DimMeasure", back_populates="trajectories")
    from_date = db.relationship(
        "DimDate",
        back_populates="trajectories_from",
        foreign_keys=[from_date_key],
    )
    to_date = db.relationship(
        "DimDate",
        back_populates="trajectories_to",
        foreign_keys=[to_date_key],
    )


class FactHealthArchetype(db.Model):
    __tablename__ = "fact_health_archetype"

    archetype_key = db.Column(db.Integer, primary_key=True)
    person_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_person.person_key"),
        nullable=False,
        index=True,
    )
    measure_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_measure.measure_key"),
        nullable=False,
        index=True,
    )
    first_date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    last_date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    num_checkups = db.Column(db.Integer, nullable=False)
    archetype_name = db.Column(db.String(64), nullable=False)
    archetype_description = db.Column(db.String(255), nullable=False)
    confidence_score = db.Column(db.Float, nullable=False)

    person = db.relationship("DimPerson", back_populates="archetypes")
    measure = db.relationship("DimMeasure", back_populates="archetypes")
    first_date = db.relationship(
        "DimDate",
        back_populates="archetypes_first",
        foreign_keys=[first_date_key],
    )
    last_date = db.relationship(
        "DimDate",
        back_populates="archetypes_last",
        foreign_keys=[last_date_key],
    )


class FactPopulationForecast(db.Model):
    __tablename__ = "fact_population_forecast"

    forecast_key = db.Column(db.Integer, primary_key=True)
    measure_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_measure.measure_key"),
        nullable=False,
        index=True,
    )
    base_date_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_date.date_key"),
        nullable=False,
        index=True,
    )
    forecast_horizon_years = db.Column(db.Integer, nullable=False)
    current_normal_count = db.Column(db.Integer, nullable=False)
    current_borderline_count = db.Column(db.Integer, nullable=False)
    current_abnormal_count = db.Column(db.Integer, nullable=False)
    forecast_normal_count = db.Column(db.Float, nullable=False)
    forecast_borderline_count = db.Column(db.Float, nullable=False)
    forecast_abnormal_count = db.Column(db.Float, nullable=False)
    normal_to_normal_prob = db.Column(db.Float, nullable=False)
    normal_to_borderline_prob = db.Column(db.Float, nullable=False)
    normal_to_abnormal_prob = db.Column(db.Float, nullable=False)
    borderline_to_normal_prob = db.Column(db.Float, nullable=False)
    borderline_to_borderline_prob = db.Column(db.Float, nullable=False)
    borderline_to_abnormal_prob = db.Column(db.Float, nullable=False)
    abnormal_to_normal_prob = db.Column(db.Float, nullable=False)
    abnormal_to_borderline_prob = db.Column(db.Float, nullable=False)
    abnormal_to_abnormal_prob = db.Column(db.Float, nullable=False)
    interpretation = db.Column(db.String(255), nullable=False)

    measure = db.relationship("DimMeasure", back_populates="population_forecasts")
    base_date = db.relationship("DimDate", back_populates="population_forecasts")


class FactDataQuality(db.Model):
    __tablename__ = "fact_data_quality"

    quality_key = db.Column(db.Integer, primary_key=True)
    check_type = db.Column(db.String(64), nullable=False, index=True)
    target_table = db.Column(db.String(64), nullable=False, index=True)
    target_field = db.Column(db.String(64), nullable=False, index=True)
    measure_key = db.Column(
        db.Integer,
        db.ForeignKey("dim_measure.measure_key"),
        nullable=True,
        index=True,
    )
    total_records = db.Column(db.Integer, nullable=False)
    missing_count = db.Column(db.Integer, nullable=False, default=0)
    missing_percent = db.Column(db.Float, nullable=False, default=0.0)
    invalid_count = db.Column(db.Integer, nullable=False, default=0)
    invalid_percent = db.Column(db.Float, nullable=False, default=0.0)
    warning_level = db.Column(db.String(32), nullable=False, index=True)
    interpretation = db.Column(db.String(255), nullable=False)

    measure = db.relationship("DimMeasure", back_populates="data_quality_checks")


class ExecutiveBrief(db.Model):
    __tablename__ = "executive_brief"

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(db.String(64), nullable=True, index=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    brief_type = db.Column(db.String(64), nullable=False, index=True)
    headline = db.Column(db.Text, nullable=True)
    executive_summary = db.Column(db.Text, nullable=True)
    why_it_matters = db.Column(db.Text, nullable=True)
    suggested_next_exploration = db.Column(db.Text, nullable=True)
    insight_packets_json = db.Column(db.Text, nullable=True)
    model_name = db.Column(db.String(255), nullable=True)
    generation_status = db.Column(db.String(32), nullable=False, default="failed", index=True)
    validation_status = db.Column(db.String(32), nullable=False, default="failed", index=True)
    diagnostic_code = db.Column(db.String(64), nullable=True)
    diagnostic_message = db.Column(db.Text, nullable=True)
    data_signature = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class ExplorationBrief(db.Model):
    __tablename__ = "exploration_brief"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False, index=True)
    metric = db.Column(db.String(64), nullable=False, index=True)
    dimension = db.Column(db.String(64), nullable=False, index=True)
    pattern = db.Column(db.String(64), nullable=True, index=True)
    key_finding = db.Column(db.Text, nullable=True)
    interpretation = db.Column(db.Text, nullable=True)
    suggested_next_investigation = db.Column(db.Text, nullable=True)
    insight_packet_json = db.Column(db.Text, nullable=True)
    model_name = db.Column(db.String(255), nullable=True)
    generation_status = db.Column(db.String(32), nullable=False, default="failed", index=True)
    validation_status = db.Column(db.String(32), nullable=False, default="failed", index=True)
    diagnostic_code = db.Column(db.String(64), nullable=True)
    diagnostic_message = db.Column(db.Text, nullable=True)
    data_signature = db.Column(db.String(64), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
