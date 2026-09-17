import pandas as pd

from core_data_loader.common.etl_common import (
    engine, ensure_sources, truncate, read_raw, raw_table_exists,
    safe_int, safe_float, safe_date,
    add_provenance_bulk, write_provenance,
)

STUDY_PREFIXES = ["immport_sdy1373", "immport_sdy1932", "immport_sdy1976"]

PROVENANCE_TABLES_OWNED = [
    "study", "study_arm", "subject", "biosample", "experiment",
    "experiment_sample", "protocol", "condition", "treatment",
    "immune_exposure", "publication",
]


def concat_tables(suffix: str, source_ids: dict) -> pd.DataFrame:
    frames = []
    for prefix in STUDY_PREFIXES:
        table = f"{prefix}_{suffix}"
        if not raw_table_exists(table):
            continue
        df = read_raw(table)
        df["_source_id"] = source_ids[prefix]
        df["_raw_table"] = table
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def main():
    source_ids = ensure_sources()
    provenance_rows = []

    truncate(
        "core.study_publication", "core.publication",
        "core.immune_exposure", "core.biosample_treatment", "core.treatment",
        "core.study_condition", "core.condition",
        "core.experiment_protocol", "core.protocol", "core.experiment_sample",
        "core.experiment", "core.biosample", "core.study_arm_subject",
        "core.subject", "core.study_arm", "core.study",
    )

    # study
    study = concat_tables("study", source_ids)
    core_study = pd.DataFrame({
        "study_accession": study["study_accession"],
        "brief_title": study["brief_title"],
        "official_title": study["official_title"],
        "brief_description": study["brief_description"],
        "actual_start_date": study["actual_start_date"].map(safe_date),
        "actual_completion_date": study["actual_completion_date"].map(safe_date),
        "actual_enrollment": study["actual_enrollment"].map(safe_int),
        "source_id": study["_source_id"],
    })
    core_study.to_sql("study", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, study, "study", "study_accession")
    print(f"core.study: {len(core_study)} rows")

    # study_arm
    arm = concat_tables("arm_or_cohort", source_ids)
    core_arm = pd.DataFrame({
        "arm_accession": arm["arm_accession"],
        "study_accession": arm["study_accession"],
        "name": arm["name"],
        "description": arm["description"],
        "type_reported": arm["type_reported"],
        "type_preferred": arm["type_preferred"],
    })
    core_arm.to_sql("study_arm", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, arm, "study_arm", "arm_accession")
    print(f"core.study_arm: {len(core_arm)} rows")

    # subject_accession is unique across all of ImmPort, not per-study, so
    # dedup across the concatenated studies before inserting
    subject = concat_tables("subject", source_ids)
    subject = subject.drop_duplicates(subset="subject_accession", keep="first")
    core_subject = pd.DataFrame({
        "subject_accession": subject["subject_accession"],
        "species": subject["species"],
        "gender": subject["gender"],
        "race": subject["race"],
        "ethnicity": subject["ethnicity"],
        "strain": subject["strain"],
        "source_id": subject["_source_id"],
    })
    core_subject.to_sql("subject", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, subject, "subject", "subject_accession")
    print(f"core.subject: {len(core_subject)} rows")

    # study_arm_subject
    a2s = concat_tables("arm_2_subject", source_ids)
    core_a2s = pd.DataFrame({
        "arm_accession": a2s["arm_accession"],
        "subject_accession": a2s["subject_accession"],
        "age_event": a2s["age_event"],
        "min_subject_age": a2s["min_subject_age"].map(safe_float),
        "max_subject_age": a2s["max_subject_age"].map(safe_float),
        "age_unit": a2s["age_unit"],
    })
    core_a2s = core_a2s.drop_duplicates(subset=["arm_accession", "subject_accession"])
    core_a2s.to_sql("study_arm_subject", engine, schema="core", if_exists="append", index=False)
    print(f"core.study_arm_subject: {len(core_a2s)} rows")

    # biosample
    biosample = concat_tables("biosample", source_ids)
    core_biosample = pd.DataFrame({
        "biosample_accession": biosample["biosample_accession"],
        "subject_accession": biosample["subject_accession"],
        "study_accession": biosample["study_accession"],
        "name": biosample["name"],
        "type": biosample["type"],
        "subtype": biosample["subtype"],
        "study_time_collected": biosample["study_time_collected"].map(safe_float),
        "study_time_collected_unit": biosample["study_time_collected_unit"],
        "study_time_t0_event": biosample["study_time_t0_event"],
        "source_id": biosample["_source_id"],
    })
    core_biosample.to_sql("biosample", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, biosample, "biosample", "biosample_accession")
    print(f"core.biosample: {len(core_biosample)} rows")

    # experiment
    experiment = concat_tables("experiment", source_ids)
    core_experiment = pd.DataFrame({
        "experiment_accession": experiment["experiment_accession"],
        "study_accession": experiment["study_accession"],
        "measurement_technique": experiment["measurement_technique"],
        "name": experiment["name"],
        "description": experiment["description"],
        "source_id": experiment["_source_id"],
    })
    core_experiment.to_sql("experiment", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, experiment, "experiment", "experiment_accession")
    print(f"core.experiment: {len(core_experiment)} rows")

    # experiment_sample (expsample joined to its biosample link, and to its
    # public-repository accession if it has one — e.g. a GEO GSM id)
    expsample = concat_tables("expsample", source_ids)
    e2b = concat_tables("expsample_2_biosample", source_ids)
    if not e2b.empty:
        e2b_first = e2b.drop_duplicates(subset="expsample_accession", keep="first")
        expsample = expsample.merge(
            e2b_first[["expsample_accession", "biosample_accession"]],
            on="expsample_accession", how="left",
        )
    else:
        expsample["biosample_accession"] = None

    repository = concat_tables("expsample_public_repository", source_ids)
    if not repository.empty:
        repo_first = repository.drop_duplicates(subset="expsample_accession", keep="first")
        expsample = expsample.merge(
            repo_first[["expsample_accession", "repository_name", "repository_accession"]],
            on="expsample_accession", how="left",
        )
    else:
        expsample["repository_name"] = None
        expsample["repository_accession"] = None

    core_expsample = pd.DataFrame({
        "expsample_accession": expsample["expsample_accession"],
        "experiment_accession": expsample["experiment_accession"],
        "biosample_accession": expsample["biosample_accession"],
        "name": expsample["name"],
        "result_schema": expsample["result_schema"],
        "repository_name": expsample["repository_name"],
        "repository_accession": expsample["repository_accession"],
    })
    core_expsample.to_sql("experiment_sample", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, expsample, "experiment_sample", "expsample_accession")
    print(f"core.experiment_sample: {len(core_expsample)} rows")

    # protocol_accession is also global across ImmPort — same dedup as subject
    protocol = concat_tables("protocol", source_ids)
    protocol = protocol.drop_duplicates(subset="protocol_accession", keep="first")
    core_protocol = pd.DataFrame({
        "protocol_accession": protocol["protocol_accession"],
        "name": protocol["name"],
        "type": protocol["type"],
        "description": protocol["description"],
    })
    core_protocol.to_sql("protocol", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, protocol, "protocol", "protocol_accession")
    print(f"core.protocol: {len(core_protocol)} rows")

    # experiment_protocol
    e2p = concat_tables("experiment_2_protocol", source_ids)
    core_e2p = e2p[["experiment_accession", "protocol_accession"]].drop_duplicates()
    core_e2p.to_sql("experiment_protocol", engine, schema="core", if_exists="append", index=False)
    print(f"core.experiment_protocol: {len(core_e2p)} rows")

    # condition / study_condition
    s2c = concat_tables("study_2_condition_or_disease", source_ids)
    if not s2c.empty:
        # Keep _raw_table/_source_id alongside the first raw row for each
        # distinct (condition_reported, condition_preferred) pair, so that
        # row can be traced back to its raw source after the condition_id
        # lookup below (condition_id itself is a DB-generated surrogate,
        # not something present in the raw data).
        distinct_full = s2c.drop_duplicates(subset=["condition_reported", "condition_preferred"], keep="first")
        distinct_conditions = distinct_full[["condition_reported", "condition_preferred"]].copy()

        # lk_disease.txt is ImmPort's own controlled vocabulary of condition
        # names -> Disease Ontology ids (e.g. "Ebola hemorrhagic fever" ->
        # "DOID:4325"); match on condition_preferred, falling back to
        # condition_reported for rows with no preferred term.
        if raw_table_exists("immport_lk_disease"):
            lk_disease = read_raw("immport_lk_disease")[["name", "disease_ontology_id"]]
            distinct_conditions = distinct_conditions.merge(
                lk_disease.rename(columns={"name": "condition_preferred", "disease_ontology_id": "ontology_id"}),
                on="condition_preferred", how="left",
            )
            missing = distinct_conditions["ontology_id"].isna()
            fallback = distinct_conditions.loc[missing, ["condition_reported"]].merge(
                lk_disease.rename(columns={"name": "condition_reported", "disease_ontology_id": "ontology_id"}),
                on="condition_reported", how="left",
            )
            distinct_conditions.loc[missing, "ontology_id"] = fallback["ontology_id"].values
        else:
            distinct_conditions["ontology_id"] = None

        distinct_conditions.to_sql("condition", engine, schema="core", if_exists="append", index=False)
        lookup = pd.read_sql_table("condition", engine, schema="core")
        s2c_with_id = s2c.merge(lookup, on=["condition_reported", "condition_preferred"], how="left")
        core_study_condition = s2c_with_id[["study_accession", "condition_id"]].drop_duplicates()
        core_study_condition.to_sql("study_condition", engine, schema="core", if_exists="append", index=False)

        distinct_with_id = distinct_full.merge(lookup, on=["condition_reported", "condition_preferred"], how="left")
        add_provenance_bulk(provenance_rows, distinct_with_id, "condition", "condition_id",
                             raw_pk_col="condition_reported")
        print(f"core.condition: {len(distinct_conditions)} rows, core.study_condition: {len(core_study_condition)} rows")
    else:
        print("core.condition / core.study_condition: no raw data")

    # treatment / biosample_treatment
    treatment = concat_tables("treatment", source_ids)
    treatment = treatment.drop_duplicates(subset="treatment_accession", keep="first")
    core_treatment = pd.DataFrame({
        "treatment_accession": treatment["treatment_accession"],
        "name": treatment["name"],
        "amount_value": treatment["amount_value"],
        "amount_unit": treatment["amount_unit"],
        "duration_value": treatment["duration_value"],
        "duration_unit": treatment["duration_unit"],
        "temperature_value": treatment["temperature_value"],
        "temperature_unit": treatment["temperature_unit"],
    })
    core_treatment.to_sql("treatment", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, treatment, "treatment", "treatment_accession")
    print(f"core.treatment: {len(core_treatment)} rows")

    # ImmPort links treatment to expsample, not directly to biosample, so
    # expsample_2_treatment needs the same expsample->biosample resolution
    # experiment_sample used above.
    e2t = concat_tables("expsample_2_treatment", source_ids)
    if not e2t.empty and not e2b.empty:
        e2b_first = e2b.drop_duplicates(subset="expsample_accession", keep="first")
        b2t = e2t.merge(
            e2b_first[["expsample_accession", "biosample_accession"]],
            on="expsample_accession", how="left",
        )
        core_b2t = b2t[["biosample_accession", "treatment_accession"]].dropna().drop_duplicates()
        core_b2t.to_sql("biosample_treatment", engine, schema="core", if_exists="append", index=False)
        print(f"core.biosample_treatment: {len(core_b2t)} rows")
    else:
        print("core.biosample_treatment: no raw data")

    # immune_exposure
    exposure = concat_tables("immune_exposure", source_ids)
    core_exposure = pd.DataFrame({
        "exposure_accession": exposure["exposure_accession"],
        "arm_accession": exposure["arm_accession"],
        "subject_accession": exposure["subject_accession"],
        "exposure_process_reported": exposure["exposure_process_reported"],
        "exposure_process_preferred": exposure["exposure_process_preferred"],
        "exposure_material_reported": exposure["exposure_material_reported"],
        "exposure_material_preferred": exposure["exposure_material_preferred"],
        "exposure_material_ontology_id": exposure["exposure_material_id"],
        "disease_reported": exposure["disease_reported"],
        "disease_preferred": exposure["disease_preferred"],
        "disease_ontology_id": exposure["disease_ontology_id"],
        "disease_stage_reported": exposure["disease_stage_reported"],
        "disease_stage_preferred": exposure["disease_stage_preferred"],
    })
    core_exposure.to_sql("immune_exposure", engine, schema="core", if_exists="append", index=False)
    add_provenance_bulk(provenance_rows, exposure, "immune_exposure", "exposure_accession")
    print(f"core.immune_exposure: {len(core_exposure)} rows")

    # publication / study_publication
    pubmed = concat_tables("study_pubmed", source_ids)
    if not pubmed.empty:
        # Same idea as condition above: keep _raw_table/_source_id on the
        # first raw row per distinct pubmed_id so the DB-generated
        # publication_id can still be traced back to it.
        distinct_pub_full = pubmed.drop_duplicates(subset="pubmed_id", keep="first")
        distinct_pub = distinct_pub_full[["pubmed_id", "title", "authors", "journal", "year"]].assign(doi=None)
        distinct_pub.to_sql("publication", engine, schema="core", if_exists="append", index=False)
        pub_lookup = pd.read_sql_table("publication", engine, schema="core")[["publication_id", "pubmed_id"]]
        s2p = pubmed.merge(pub_lookup, on="pubmed_id", how="left")
        core_study_pub = s2p[["study_accession", "publication_id"]].drop_duplicates()
        core_study_pub.to_sql("study_publication", engine, schema="core", if_exists="append", index=False)

        pub_with_id = distinct_pub_full.merge(pub_lookup, on="pubmed_id", how="left")
        add_provenance_bulk(provenance_rows, pub_with_id, "publication", "publication_id", raw_pk_col="pubmed_id")
        print(f"core.publication: {len(distinct_pub)} rows, core.study_publication: {len(core_study_pub)} rows")
    else:
        print("core.publication / core.study_publication: no raw data")

    write_provenance(provenance_rows, PROVENANCE_TABLES_OWNED)

    print("Finished ImmPort core ETL.")


if __name__ == "__main__":
    main()
