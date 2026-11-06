"""Curated description of the StarRocks tables, served by ``describe_schema``.

The raw schema (``db_summary``, ``table_overview``) says which columns exist,
not which table to use, how to join them, which partition to prune or which
values a column holds — which is where SQL written by a model goes wrong. This
catalog says it, per topic.

It is data, not prose, so the unit tests can check it against the ETL, which
owns the schema: every table and column below must exist in the ETL's
create-table DDL, every enum must match an ETL data QA dictionary, and every
table named in an example query must be in the catalog.
"""

from dataclasses import dataclass, field

from .enums import ENUMS

TENANT_DB = "<tenant>_tenant"
SHARED_DB = "radiant"


@dataclass(frozen=True)
class Table:
    db: str
    name: str
    purpose: str
    # column -> short description; only the useful ones, not the full DDL.
    columns: dict[str, str]
    keys: str = ""
    partition: str = ""
    # column -> enums.ENUMS key, rendered as the stored values.
    enums: dict[str, str] = field(default_factory=dict)

    @property
    def qualified(self) -> str:
        return f"{self.db}.{self.name}"


@dataclass(frozen=True)
class Topic:
    title: str
    summary: str
    tables: tuple[Table, ...]
    rules: tuple[str, ...] = ()
    queries: tuple[tuple[str, str], ...] = ()


_PARTITION_RULE = (
    "Every query on a table partitioned by `part` carries `<alias>.part = <part>` as an equality, "
    "next to seq_id / task_id. Take `part` and `variant_part` from get_case_context's "
    "occurrence_keys, from the key of the right data type; never compute them. Without it "
    "StarRocks scans every partition of the tenant and the query can time out."
)
ARRAY_RULE = (
    "Columns marked (array) hold lists. Filter with array_contains(col, 'value') or "
    "arrays_overlap(col, ['a', 'b']). To count or group by element, unnest in the FROM clause: "
    "`FROM <table> v, unnest(v.col) AS u(value)`, then GROUP BY u.value. StarRocks has no "
    "LATERAL VIEW / explode."
)
_TENANT_RULE = (
    "`<tenant>` is a placeholder for the tenant code returned by get_case_context. Another "
    "tenant's database answers 0 rows without error: re-check the prefix before concluding "
    "that data is missing."
)

GERMLINE_SNV_OCCURRENCE = Table(
    TENANT_DB, "germline__snv__occurrence",
    "One row per variant called in a germline sequencing.",
    {
        "seq_id": "sequencing", "task_id": "analysis task", "locus_id": "variant id",
        "zygosity": "genotype", "ad_ratio": "alt allele ratio", "ad_alt": "alt depth",
        "ad_total": "total depth", "gq": "genotype quality", "filter": "PASS or the failed filter",
        "info_qd": "quality by depth",
        "father_zygosity": "father's genotype (trio)", "mother_zygosity": "mother's genotype (trio)",
        "transmission_mode": "inferred mode of transmission", "parental_origin": "parent the allele comes from",
        "exomiser_acmg_classification": "Exomiser ACMG class", "exomiser_acmg_evidence": "ACMG codes (array)",
        "exomiser_variant_score": "Exomiser variant score", "exomiser_gene_combined_score": "Exomiser gene score",
        "exomiser_moi": "Exomiser mode of inheritance", "has_comphet": "has a compound heterozygous partner",
    },
    keys="part, seq_id, task_id, locus_id",
    partition="part",
    enums={
        "zygosity": "zygosity", "father_zygosity": "zygosity", "mother_zygosity": "zygosity",
        "transmission_mode": "transmission_mode",
        "exomiser_acmg_classification": "exomiser_acmg_classification",
    },
)
SOMATIC_SNV_OCCURRENCE = Table(
    TENANT_DB, "somatic__snv__occurrence",
    "One row per variant called in a tumor (tumor-normal or tumor-only).",
    {
        "tumor_seq_id": "tumor sequencing (the key's seq_id)", "normal_seq_id": "normal sequencing, NULL in tumor-only",
        "task_id": "analysis task", "locus_id": "variant id", "filter": "PASS or the failed filter",
        "tumor_ad_ratio": "tumor alt allele ratio", "tumor_ad_alt": "tumor alt depth",
        "tumor_ad_total": "tumor depth", "normal_ad_ratio": "normal alt allele ratio",
        "info_hotspot": "cancer hotspot flag",
    },
    keys="part, task_id, tumor_seq_id, locus_id",
    partition="part",
)
SNV_VARIANT = Table(
    TENANT_DB, "snv__variant_partitioned",
    "The variant catalog: one row per locus_id with its picked annotation and frequencies. "
    "Use it, never the unpartitioned snv__variant.",
    {
        "locus_id": "variant id", "chromosome": "chromosome", "start": "position", "reference": "ref allele",
        "alternate": "alt allele", "hgvsg": "genomic HGVS", "hgvsc": "coding HGVS", "hgvsp": "protein HGVS",
        "symbol": "gene", "transcript_id": "picked transcript", "aa_change": "amino-acid change",
        "variant_class": "variant class", "vep_impact": "VEP impact", "impact_score": "impact rank",
        "consequences": "consequences (array)", "is_mane_select": "MANE Select transcript",
        "rsnumber": "dbSNP id", "clinvar_interpretation": "ClinVar classifications (array)",
        "omim_inheritance_code": "OMIM inheritance (array)", "gnomad_v3_af": "gnomAD v3 AF, NULL when absent",
        "cmc_tier": "COSMIC CMC tier",
    },
    keys="locus_id",
    partition="part (= the key's variant_part)",
    enums={
        "variant_class": "variant_class", "vep_impact": "vep_impact",
        "clinvar_interpretation": "clinvar_interpretation", "cmc_tier": "cosmic_tier",
    },
)
EXOMISER = Table(
    TENANT_DB, "exomiser",
    "Exomiser ranking of the variants of a germline sequencing.",
    {
        "seq_id": "sequencing", "task_id": "analysis task", "locus_id": "variant id", "rank": "gene rank",
        "symbol": "gene", "moi": "mode of inheritance", "variant_score": "variant score",
        "gene_combined_score": "gene score", "acmg_evidence": "ACMG codes (array)",
    },
    keys="part, seq_id, task_id, locus_id",
    partition="part",
)
COMPHET = Table(
    TENANT_DB, "germline__snv__comphet",
    "Compound heterozygous pairs of a germline sequencing.",
    {"seq_id": "sequencing", "task_id": "analysis task", "locus_id": "variant id",
     "partner_locus_id": "the other variant of the pair", "symbol": "gene"},
    keys="part, seq_id, task_id, locus_id",
    partition="part",
)
_CNV_COLUMNS = {
    "seq_id": "sequencing", "task_id": "analysis task", "cnv_id": "CNV id", "chromosome": "chromosome",
    "start": "start", "end": "end", "type": "CNV type", "length": "length (bp)", "cn": "copy number",
    "symbol": "genes overlapped (array: array_contains(symbol, 'GENE'))", "nb_genes": "number of genes",
    "cytoband": "cytobands (array)", "filter": "PASS or the failed filter", "quality": "call quality",
    "gnomad_sf": "gnomAD-SV frequency",
}
GERMLINE_CNV_OCCURRENCE = Table(
    TENANT_DB, "germline__cnv__occurrence", "One row per CNV called in a germline sequencing.",
    _CNV_COLUMNS, keys="part, seq_id, task_id, cnv_id", partition="part", enums={"type": "cnv_type"},
)
SOMATIC_CNV_OCCURRENCE = Table(
    TENANT_DB, "somatic__cnv__occurrence",
    "One row per CNV called in a tumor; seq_id is the tumor sequencing.",
    {**_CNV_COLUMNS, "cnf": "copy number fraction", "mcn": "minor copy number", "maf": "minor allele fraction"},
    keys="part, seq_id, task_id, cnv_id", partition="part", enums={"type": "cnv_type"},
)
GERMLINE_CNV_FREQUENCY = Table(
    TENANT_DB, "germline__cnv__variant_frequency", "Frequency of each germline CNV in the tenant.",
    {"cnv_id": "CNV id", "pc_wgs": "carriers (WGS)", "pn_wgs": "sequenced patients (WGS)",
     "pf_wgs": "carrier frequency (WGS)", "pf_wgs_affected": "frequency in affected patients"},
    keys="cnv_id",
)
CONSEQUENCE_FILTER = Table(
    SHARED_DB, "snv__consequence_filter_partitioned",
    "Picked consequence and in-silico scores per locus_id, for filters on predictors.",
    {
        "locus_id": "variant id", "symbol": "gene", "consequence": "consequence", "biotype": "biotype",
        "vep_impact": "VEP impact", "is_deleterious": "deleterious flag", "cadd_phred": "CADD phred",
        "revel_score": "REVEL", "spliceai_ds": "SpliceAI delta score", "sift_pred": "SIFT",
        "polyphen2_hvar_pred": "PolyPhen-2", "gnomad_pli": "pLI", "gnomad_loeuf": "LOEUF",
    },
    keys="part, locus_id",
    partition="part (= the key's part)",
    enums={"vep_impact": "vep_impact"},
)
CONSEQUENCE = Table(
    SHARED_DB, "snv__consequence",
    "Every transcript of every variant. Only for what the filter table lacks.",
    {"locus_id": "variant id", "transcript_id": "transcript", "is_picked": "picked transcript",
     "is_mane_select": "MANE Select", "exon_rank": "exon", "exon_total": "exon count",
     "spliceai_type": "SpliceAI event type"},
    keys="locus_id, symbol, transcript_id",
)
CLINVAR = Table(
    SHARED_DB, "clinvar", "ClinVar record per locus_id.",
    {"locus_id": "variant id", "name": "ClinVar name", "interpretations": "classifications (array)",
     "conditions": "conditions (array)", "clnrevstat": "review status (array)"},
    keys="locus_id",
)
OMIM_GENE_PANEL = Table(
    SHARED_DB, "omim_gene_panel", "OMIM conditions of each gene.",
    {"symbol": "gene", "panel": "condition name", "inheritance_code": "inheritance (array)",
     "omim_gene_id": "OMIM gene id", "omim_phenotype_id": "OMIM phenotype id"},
    keys="symbol, panel",
)
HPO_GENE_PANEL = Table(
    SHARED_DB, "hpo_gene_panel", "HPO terms associated with each gene.",
    {"symbol": "gene", "hpo_term_id": "HPO id (HP:...)", "hpo_term_name": "HPO name"},
    keys="symbol, panel",
)
HPO_TERM = Table(SHARED_DB, "hpo_term", "HPO terms.", {"id": "HP:...", "name": "term name"}, keys="id")
MONDO_TERM = Table(SHARED_DB, "mondo_term", "MONDO terms.", {"id": "MONDO:...", "name": "term name"}, keys="id")
ORPHANET_GENE_PANEL = Table(
    SHARED_DB, "orphanet_gene_panel", "Orphanet disorders of each gene.",
    {"symbol": "gene", "panel": "disorder name", "disorder_id": "Orphanet id", "inheritance_code": "inheritance (array)"},
    keys="symbol, panel",
)
GNOMAD_CONSTRAINT = Table(
    SHARED_DB, "gnomad_constraint", "gnomAD constraint per transcript.",
    {"transcript_id": "transcript", "pli": "pLI", "loeuf": "LOEUF"},
)
GERMLINE_SNV_FREQUENCY = Table(
    TENANT_DB, "germline__snv__variant_frequency",
    "Germline frequency of each variant in the tenant. The same values are on snv__variant_partitioned "
    "with a germline_ prefix: prefer that table when you already join it.",
    {"locus_id": "variant id", "pc_wgs": "carriers (WGS)", "pn_wgs": "sequenced patients (WGS)",
     "pf_wgs": "carrier frequency (WGS)", "hom_wgs": "HOM/HEM carriers (WGS)",
     "pf_wgs_affected": "frequency in affected patients", "pf_wgs_not_affected": "frequency in non-affected",
     "pf_wxs": "carrier frequency (WXS)"},
    keys="locus_id",
)
SNV_VARIANT_FREQUENCIES = Table(
    TENANT_DB, "snv__variant_partitioned",
    "Frequency columns of the variant catalog (same partition rule as in germline_snv).",
    {
        "locus_id": "variant id", "gnomad_v3_af": "gnomAD v3 AF, NULL when absent", "topmed_af": "TOPMed AF",
        "tg_af": "1000 Genomes AF", "germline_pc_wgs": "tenant carriers (WGS)",
        "germline_pn_wgs": "tenant sequenced patients (WGS)", "germline_pf_wgs": "tenant carrier frequency (WGS)",
        "germline_af_wgs": "approximate allele frequency (WGS)", "germline_hom_wgs": "HOM/HEM carriers (WGS)",
        "germline_pf_wgs_affected": "frequency in affected", "germline_pf_wgs_not_affected": "frequency in non-affected",
        "germline_pf_wxs": "carrier frequency (WXS)", "somatic_pf_tn_wgs": "somatic frequency, tumor-normal",
        "somatic_pf_to_wgs": "somatic frequency, tumor-only",
    },
    keys="locus_id",
    partition="part (= the key's variant_part)",
)

TOPICS: dict[str, Topic] = {
    "germline_snv": Topic(
        "Germline SNVs",
        "Occurrences of a germline sequencing, joined to the variant catalog. Prefer prioritize_snv; "
        "use SQL only for what it cannot express.",
        (GERMLINE_SNV_OCCURRENCE, SNV_VARIANT, EXOMISER, COMPHET),
        rules=(
            _TENANT_RULE,
            _PARTITION_RULE,
            "Join the catalog with `v.locus_id = o.locus_id AND v.part = <variant_part>`.",
            "The parents' genotypes are already on the proband's row (father_zygosity, mother_zygosity): "
            "no need to query the parents' occurrences for a trio.",
            "gnomad_v3_af is NULL for a variant absent from gnomAD: write "
            "`(v.gnomad_v3_af IS NULL OR v.gnomad_v3_af < 0.01)` for rare variants.",
            "Enum values are case-sensitive: use the stored values listed below.",
            ARRAY_RULE,
        ),
        queries=(
            ("Rare pathogenic or de novo candidates of one sequencing",
             "SELECT o.locus_id, v.symbol, v.hgvsg, v.gnomad_v3_af, v.clinvar_interpretation, o.zygosity,\n"
             "       o.father_zygosity, o.mother_zygosity, o.transmission_mode, o.exomiser_acmg_classification\n"
             "FROM <tenant>_tenant.germline__snv__occurrence o\n"
             "JOIN <tenant>_tenant.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = <variant_part>\n"
             "WHERE o.seq_id = <seq_id> AND o.task_id = <task_id> AND o.part = <part>\n"
             "  AND (v.gnomad_v3_af IS NULL OR v.gnomad_v3_af < 0.01)\n"
             "  AND (array_contains(v.clinvar_interpretation, 'Pathogenic')\n"
             "       OR o.transmission_mode = 'autosomal_dominant_de_novo')\n"
             "LIMIT 100;"),
            ("Rare variants of one sequencing counted by consequence (unnest of an array column)",
             "SELECT c.consequence, COUNT(*) AS n\n"
             "FROM <tenant>_tenant.germline__snv__occurrence o\n"
             "JOIN <tenant>_tenant.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = <variant_part>,\n"
             "     unnest(v.consequences) AS c(consequence)\n"
             "WHERE o.seq_id = <seq_id> AND o.task_id = <task_id> AND o.part = <part>\n"
             "  AND (v.gnomad_v3_af IS NULL OR v.gnomad_v3_af < 0.01)\n"
             "GROUP BY c.consequence ORDER BY n DESC;"),
            ("SNV counts shared by a trio (Venn regions)",
             "WITH occ AS (\n"
             "  SELECT locus_id,\n"
             "    MAX(CASE WHEN seq_id = <p_seq> AND task_id = <p_task> THEN 1 ELSE 0 END) p,\n"
             "    MAX(CASE WHEN seq_id = <f_seq> AND task_id = <f_task> THEN 1 ELSE 0 END) f,\n"
             "    MAX(CASE WHEN seq_id = <m_seq> AND task_id = <m_task> THEN 1 ELSE 0 END) m\n"
             "  FROM <tenant>_tenant.germline__snv__occurrence\n"
             "  WHERE (seq_id = <p_seq> AND task_id = <p_task> AND part = <p_part>)\n"
             "     OR (seq_id = <f_seq> AND task_id = <f_task> AND part = <f_part>)\n"
             "     OR (seq_id = <m_seq> AND task_id = <m_task> AND part = <m_part>)\n"
             "  GROUP BY locus_id)\n"
             "SELECT p, f, m, COUNT(*) AS n FROM occ GROUP BY p, f, m;"),
        ),
    ),
    "somatic_snv": Topic(
        "Somatic SNVs",
        "Occurrences of a tumor sequencing, joined to the variant catalog. Prefer prioritize_snv.",
        (SOMATIC_SNV_OCCURRENCE, SNV_VARIANT),
        rules=(
            _TENANT_RULE,
            _PARTITION_RULE,
            "The somatic occurrence key is tumor_seq_id, not seq_id. normal_seq_id is NULL in tumor-only.",
            "Join the catalog with `v.locus_id = o.locus_id AND v.part = <variant_part>`.",
            ARRAY_RULE,
        ),
        queries=(
            ("High-impact PASS variants of a tumor",
             "SELECT o.locus_id, v.symbol, v.hgvsp, v.vep_impact, o.tumor_ad_ratio, o.info_hotspot\n"
             "FROM <tenant>_tenant.somatic__snv__occurrence o\n"
             "JOIN <tenant>_tenant.snv__variant_partitioned v ON v.locus_id = o.locus_id AND v.part = <variant_part>\n"
             "WHERE o.tumor_seq_id = <seq_id> AND o.task_id = <task_id> AND o.part = <part>\n"
             "  AND o.filter = 'PASS' AND v.vep_impact = 'HIGH'\n"
             "LIMIT 100;"),
        ),
    ),
    "cnv": Topic(
        "CNVs",
        "Germline and somatic CNV calls. Prefer case_cnv. The CNV task of a sequencing is usually "
        "not its SNV task: take (seq_id, task_id, part) from the germline_cnv or somatic_cnv key.",
        (GERMLINE_CNV_OCCURRENCE, SOMATIC_CNV_OCCURRENCE, GERMLINE_CNV_FREQUENCY),
        rules=(
            _TENANT_RULE,
            _PARTITION_RULE,
            "symbol is an array of the genes overlapped: filter with array_contains(symbol, 'GENE').",
            ARRAY_RULE,
        ),
        queries=(
            ("Rare PASS CNVs of one sequencing, largest first",
             "SELECT cnv_id, chromosome, start, end, type, length, cn, symbol, nb_genes, gnomad_sf\n"
             "FROM <tenant>_tenant.germline__cnv__occurrence\n"
             "WHERE seq_id = <seq_id> AND task_id = <task_id> AND part = <part>\n"
             "  AND filter = 'PASS' AND (gnomad_sf IS NULL OR gnomad_sf < 0.01)\n"
             "ORDER BY length DESC LIMIT 50;"),
        ),
    ),
    "annotations": Topic(
        "Annotations and reference data",
        "Predictors, transcripts, ClinVar, gene panels and ontologies, in the shared `radiant` "
        "database. Prefer variant_details for one variant.",
        (CONSEQUENCE_FILTER, CONSEQUENCE, CLINVAR, OMIM_GENE_PANEL, HPO_GENE_PANEL, ORPHANET_GENE_PANEL,
         HPO_TERM, MONDO_TERM, GNOMAD_CONSTRAINT),
        rules=(
            "Shared tables are always qualified with `radiant.`.",
            "Filter occurrences on predictors with "
            "`LEFT SEMI JOIN radiant.snv__consequence_filter_partitioned cf ON cf.locus_id = o.locus_id "
            "AND cf.part = o.part AND (<condition>)`.",
            "snv__consequence holds every transcript: add `is_picked = true` for the picked one.",
            ARRAY_RULE,
        ),
        queries=(
            ("Genes associated with the proband's HPO terms",
             "SELECT DISTINCT symbol FROM radiant.hpo_gene_panel WHERE hpo_term_id IN ('HP:0001250', 'HP:0001263');"),
            ("OMIM conditions of a gene",
             "SELECT panel, omim_phenotype_id, inheritance_code FROM radiant.omim_gene_panel WHERE symbol = 'SCN1A';"),
        ),
    ),
    "frequencies": Topic(
        "Frequencies",
        "External (gnomAD, TOPMed, 1000 Genomes) and tenant frequencies. Prefer variant_details for one "
        "variant and variant_carriers for the cases that carry it.",
        (SNV_VARIANT_FREQUENCIES, GERMLINE_SNV_FREQUENCY, GERMLINE_CNV_FREQUENCY),
        rules=(
            _TENANT_RULE,
            "pc = carriers, pn = sequenced patients, pf = pc / pn, hom = HOM or HEM carriers, af = "
            "approximate allele frequency. _wgs and _wxs split by sequencing strategy; _affected and "
            "_not_affected by affected status.",
            "External frequencies are NULL when the variant is absent from the source.",
        ),
        queries=(
            ("Tenant and gnomAD frequencies of variants",
             "SELECT locus_id, gnomad_v3_af, germline_pc_wgs, germline_pn_wgs, germline_pf_wgs\n"
             "FROM <tenant>_tenant.snv__variant_partitioned\n"
             "WHERE part = <variant_part> AND locus_id IN (<locus_id>, <locus_id>);"),
        ),
    ),
}


def render(topic_name: str) -> str:
    """Markdown text of one topic."""
    topic = TOPICS[topic_name]
    lines = [f"# {topic.title}", "", topic.summary, ""]
    if topic.rules:
        lines += ["## Rules", ""] + [f"- {rule}" for rule in topic.rules] + [""]
    lines += ["## Tables", ""]
    for table in topic.tables:
        lines.append(f"### {table.qualified}")
        lines.append("")
        lines.append(table.purpose)
        details = []
        if table.keys:
            details.append(f"Keys: {table.keys}.")
        if table.partition:
            details.append(f"Partitioned by {table.partition}.")
        if details:
            lines.append(" ".join(details))
        lines.append("")
        for column, description in table.columns.items():
            line = f"- `{column}`: {description}"
            enum = table.enums.get(column)
            if enum:
                line += ". Values: " + ", ".join(f"'{v}'" for v in ENUMS[enum])
            lines.append(line)
        lines.append("")
    if topic.queries:
        lines += ["## Example queries", ""]
        for title, sql in topic.queries:
            lines += [f"{title}:", "", "```sql", sql, "```", ""]
    return "\n".join(lines).rstrip() + "\n"
