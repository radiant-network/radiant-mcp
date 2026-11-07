"""VEP consequences grouped into the classes the oncoprint and lollipop draw."""

from ..sql import as_list

# VEP consequence -> class, most severe first.
CONSEQUENCE_CLASSES = (
    ("truncating", {"stop_gained", "frameshift_variant", "start_lost", "stop_lost", "transcript_ablation"}),
    ("splice", {"splice_acceptor_variant", "splice_donor_variant", "splice_donor_5th_base_variant",
                "splice_donor_region_variant", "splice_region_variant"}),
    ("missense", {"missense_variant"}),
    ("inframe", {"inframe_insertion", "inframe_deletion", "protein_altering_variant"}),
)


def consequence_class(consequences) -> str:
    found = set(as_list(consequences) if isinstance(consequences, str) else consequences or [])
    for name, members in CONSEQUENCE_CLASSES:
        if found & members:
            return name
    return "other"
