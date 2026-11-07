"""Clinical rules of the RAD-252 tools, in one place.

They decide what ``second_hit``, ``cohort_carriers`` and ``genotype_phenotype``
report to geneticists, so they are fixed by the spec
(hub ``docs/features/RAD-252-david-p2-tools.md``) and restated in each tool's
output. Every stored value used here is checked against ``enums.ENUMS`` by
the unit tests.
"""

# A variant is rare when gnomAD v3 has it below this allele frequency, or not at all.
RARE_MAX_AF = 0.01

# Impacts that make a rare SNV a candidate on their own.
QUALIFYING_IMPACTS = ("HIGH", "MODERATE")

# Pathogenic or likely pathogenic, from three sources.
CLINVAR_PLP = ("Pathogenic", "Likely_pathogenic")
EXOMISER_PLP = ("pathogenic", "likely_pathogenic")
# Germline interpretations saved in the portal store LOINC answer codes.
INTERPRETATION_PLP = ("LA6668-3", "LA26332-9")

# CNV types that count as a second event.
GERMLINE_SECOND_HIT_CNV_TYPES = ("LOSS",)
SOMATIC_SECOND_HIT_CNV_TYPES = ("LOSS", "CNLOH")

PASS = "PASS"
HET = "HET"
# Calls with an alternate allele. A family member can also have WT rows.
ALT_ZYGOSITIES = ("HET", "HOM", "HEM")
# An SNV inside a heterozygous deletion keeps one allele: the caller reports it HOM (or HEM).
HEMIZYGOUS_CALLS = ("HOM", "HEM")

# staging_sequencing_experiment.task_type of a germline SNV annotation task.
GERMLINE_SNV_TASK_TYPE = "radiant_germline_annotation"

# Portal observations: an observed (positive) HPO phenotype.
PHENOTYPE_OBSERVATION_CODE = "phenotype"
POSITIVE_INTERPRETATION_CODE = "positive"

PLP_DEFINITION = (
    "P/LP = ClinVar Pathogenic or Likely_pathogenic, or Exomiser pathogenic or likely_pathogenic, "
    "or a portal interpretation classified Pathogenic (LA6668-3) or Likely pathogenic (LA26332-9)."
)
RARE_DEFINITION = f"rare = gnomAD v3 AF < {RARE_MAX_AF:g} or absent from gnomAD."
