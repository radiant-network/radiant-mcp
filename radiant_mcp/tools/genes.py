"""Gene and ontology tools: ``gene_info`` and ``lookup_terms``."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from . import _api, _sql
from ._api import RadiantApi, clamp, resolve_tenant

ONTOLOGIES = {
    "hpo": ("hpo", "hpo_term_auto_complete"),
    "mondo": ("mondo", "mondo_term_auto_complete"),
}
TERMS_MAX_LIMIT = 50
_HPO_TERMS_SHOWN = 100
_WORKERS = 3


def resolve_gene(api: RadiantApi, tenant: str, symbol: str) -> Optional[dict]:
    """``{symbol, ensembl_gene_id}`` of a gene name, through the portal gene
    search (it knows the current HGNC symbols); None when unknown.

    An exact match on the symbol wins (case-insensitive), otherwise the first
    result: the search also matches aliases and Ensembl ids.
    """
    from radiant_python.models.gene_search_body import GeneSearchBody

    _sql.symbol(symbol)
    results = api.call("genes", "gene_search", tenant=tenant, gene_search_body=GeneSearchBody(inputs=[symbol])) or []
    results = [r for r in results if r.get("symbol")]
    if not results:
        return None
    exact = [r for r in results if r["symbol"].lower() == symbol.lower()]
    return (exact or results)[0]


def _gene_queries(gene: str, ensembl_gene_id: Optional[str], tenant: str) -> dict[str, tuple[str, _sql.Params]]:
    shared, tenant_db = _sql.shared_db(), _sql.tenant_db(tenant)
    queries: dict[str, tuple[str, _sql.Params]] = {}

    def add(name: str, build) -> None:
        p = _sql.Params()
        queries[name] = (build(p), p)

    add("omim", lambda p: (
        f"SELECT panel AS condition_name, omim_phenotype_id, omim_gene_id, inheritance_code "
        f"FROM {shared}.omim_gene_panel WHERE symbol = {p(gene)} ORDER BY panel LIMIT 50"))
    add("hpo", lambda p: (
        f"SELECT hpo_term_id, hpo_term_name FROM {shared}.hpo_gene_panel "
        f"WHERE symbol = {p(gene)} ORDER BY hpo_term_id"))
    add("orphanet", lambda p: (
        f"SELECT panel AS disorder, disorder_id, inheritance_code FROM {shared}.orphanet_gene_panel "
        f"WHERE symbol = {p(gene)} ORDER BY panel LIMIT 50"))
    add("ddd", lambda p: f"SELECT panel FROM {shared}.ddd_gene_panel WHERE symbol = {p(gene)} ORDER BY panel")
    add("cosmic", lambda p: f"SELECT panel FROM {shared}.cosmic_gene_panel WHERE symbol = {p(gene)} ORDER BY panel")
    add("tenant_panels", lambda p: (
        f"SELECT DISTINCT panel FROM {tenant_db}.gene_panel_mv WHERE symbol = {p(gene)} ORDER BY panel"))
    if ensembl_gene_id:
        # gnomAD constraint is per transcript: the gene's transcripts come from
        # the Ensembl exon table (keyed by gene_id); versions are ignored.
        add("constraint", lambda p: (
            "SELECT DISTINCT c.transcript_id, c.pli, c.loeuf "
            f"FROM (SELECT DISTINCT split_part(t.transcript_id, '.', 1) AS transcript_id "
            f"      FROM {shared}.ensembl_exon_by_gene e, unnest(e.transcript_ids) AS t(transcript_id) "
            f"      WHERE e.gene_id = {p(ensembl_gene_id)}) g "
            f"JOIN {shared}.gnomad_constraint c ON split_part(c.transcript_id, '.', 1) = g.transcript_id "
            "ORDER BY c.pli DESC LIMIT 10"))
    return queries


def _gene_info(api: RadiantApi, token: str, gene: str, tenant: Optional[str]) -> dict:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err
    resolved = resolve_gene(api, tenant_code, gene)
    if resolved is None:
        return {"error": f"Gene {gene!r} not found.", "status": 404}
    symbol = _sql.symbol(resolved["symbol"])
    queries = _gene_queries(symbol, resolved.get("ensembl_gene_id"), tenant_code)

    def _fetch(item: tuple[str, tuple[str, _sql.Params]]) -> tuple[str, Any, Optional[str]]:
        name, (sql, params) = item
        try:
            return name, _sql.run(token, sql, params), None
        except _sql.SqlError as exc:
            return name, None, f"{name}: {exc}"

    result: dict[str, Any] = {
        "tenant": tenant_code,
        "symbol": symbol,
        "ensembl_gene_id": resolved.get("ensembl_gene_id"),
    }
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        rows = {}
        for name, data, warning in pool.map(_fetch, queries.items()):
            if warning:
                warnings.append(warning)
            else:
                rows[name] = data

    if "omim" in rows:
        result["omim_conditions"] = [
            {**r, "inheritance_code": _sql.as_list(r.get("inheritance_code"))} for r in rows["omim"]
        ]
    if "hpo" in rows:
        result["hpo_terms_count"] = len(rows["hpo"])
        result["hpo_terms"] = rows["hpo"][:_HPO_TERMS_SHOWN]
    if "orphanet" in rows:
        result["orphanet_disorders"] = [
            {**r, "inheritance_code": _sql.as_list(r.get("inheritance_code"))} for r in rows["orphanet"]
        ]
    if "ddd" in rows:
        result["ddd_panels"] = [r["panel"] for r in rows["ddd"]]
    if "cosmic" in rows:
        result["cosmic_panels"] = [r["panel"] for r in rows["cosmic"]]
    if "tenant_panels" in rows:
        result["tenant_gene_panels"] = [r["panel"] for r in rows["tenant_panels"]]
    if "constraint" in rows:
        result["gnomad_constraint"] = rows["constraint"]
    if warnings:
        result["warnings"] = warnings
    return result


def _lookup_terms(api: RadiantApi, _token: str, ontology: str, query: str, tenant: Optional[str], limit: int) -> dict:
    tenant_code, err = resolve_tenant(api, tenant)
    if err:
        return err
    api_name, operation = ONTOLOGIES[ontology]
    matches = api.call(api_name, operation, tenant=tenant_code, prefix=query, limit=str(limit)) or []
    terms = []
    for match in matches:
        source = match.get("source") or {}
        if source.get("id"):
            terms.append({"id": source["id"], "name": source.get("name")})
    return {"tenant": tenant_code, "ontology": ontology, "query": query, "terms": terms}


# -- tools -------------------------------------------------------------------

async def gene_info(symbol: str, tenant: Optional[str] = None) -> dict:
    """What is known about a gene, independently of any patient.

    Returns ``omim_conditions`` (condition, OMIM ids, inheritance),
    ``hpo_terms`` (the HPO phenotypes associated with the gene, with their
    count), ``orphanet_disorders``, ``ddd_panels``, ``cosmic_panels`` (cancer
    gene census membership), ``tenant_gene_panels`` (the tenant's own panels
    that contain the gene) and ``gnomad_constraint`` (pLI and LOEUF per
    transcript, highest pLI first). Sections that fail are listed in
    ``warnings``.

    Args:
        symbol: Gene symbol, alias or Ensembl gene id (e.g. SCN1A, ENSG00000144285).
        tenant: Tenant code (auto-resolved when the user has only one).
    """
    try:
        _sql.symbol(symbol)
    except _sql.InvalidInput as exc:
        return {"error": str(exc)}
    return await _api.run(_gene_info, symbol, tenant)


async def lookup_terms(ontology: str, query: str, tenant: Optional[str] = None, limit: int = 10) -> dict:
    """Find HPO or MONDO terms by name or id, with their identifiers.

    Use it to name an HPO code, or to find the MONDO id of a condition (e.g.
    for the Condition field of an interpretation draft). Never invent an id:
    take it from this tool.

    Args:
        ontology: ``hpo`` or ``mondo``.
        query: Words of the term name, or the beginning of an id (e.g.
            "seizure", "HP:00012", "Dravet").
        tenant: Tenant code (auto-resolved when the user has only one).
        limit: Max terms returned (1–50, default 10).
    """
    if ontology not in ONTOLOGIES:
        return {"error": f"Unknown ontology {ontology!r}. Allowed: {', '.join(ONTOLOGIES)}."}
    if not (query or "").strip():
        return {"error": "Provide a non-empty `query`."}
    return await _api.run(_lookup_terms, ontology, query.strip(), tenant, clamp(limit, 1, TERMS_MAX_LIMIT))
