from app import db


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
