"""Gene resolution through the portal gene search.

The search matches the current HGNC symbol and the Ensembl gene id, not the
aliases (``ensembl_gene.alias`` is not read): HER2 or P53 are not found.
"""


from .. import sql as _sql
from ..portal_api import RadiantApi


def resolve_gene(api: RadiantApi, tenant: str, symbol: str) -> dict | None:
    """``{symbol, ensembl_gene_id}`` of a gene name, through the portal gene
    search (it knows the current HGNC symbols); None when unknown.

    An exact match on the symbol wins (case-insensitive), otherwise the first
    result (an Ensembl id).
    """
    from radiant_python.models.gene_search_body import GeneSearchBody

    _sql.symbol(symbol)
    results = api.call("genes", "gene_search", tenant=tenant, gene_search_body=GeneSearchBody(inputs=[symbol])) or []
    results = [r for r in results if r.get("symbol")]
    if not results:
        return None
    exact = [r for r in results if r["symbol"].lower() == symbol.lower()]
    return (exact or results)[0]


def not_found_message(gene: str) -> str:
    """What the agent reads when ``resolve_gene`` finds nothing: how to retry."""
    return (f"Gene {gene!r} not found. Use the official HGNC symbol (e.g. ERBB2, not HER2) or the Ensembl "
            "gene id: aliases are not resolved.")
