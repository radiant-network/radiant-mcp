# Generative UI — rendering components in the chat

Some answers are tables, not prose. `search_variants` is the first tool that
answers with a *rendered* result: a variant table whose first column deep-links
each variant into the Radiant portal.

There is no single way to do this, because the two chat clients we care about
render tool results very differently. So one tool result carries the same answer
three ways, and each client picks the representation it understands.

| Client | Reads | Renders with |
|---|---|---|
| radiant-portal assistant panel | `structuredContent.blocks` | real shadcn components (`<BlockRenderer>`) |
| LibreChat, Claude Desktop, anything else (incl. the model) | the text content | Markdown |

```
                         ┌─ TextContent       → Markdown table (model reads this; clickable links)
search_variants result ──┤
                         └─ structuredContent → {"version":1,"blocks":[…]}  (portal renders it)
```

**There is no mcp-ui `ui://` iframe.** We built one and removed it — see
[Why not an mcp-ui resource](#why-not-an-mcp-ui-resource).

## The block contract

`radiant_mcp/blocks.py` is the source of truth on the server side; the portal's
`frontend/apps/assistant/src/engine/wire.ts` validates the same shape. Keep them
in sync.

```jsonc
{
  "version": 1,
  "blocks": [
    { "type": "text", "content": "Found 12 variants for gene BRCA1." },
    {
      "type": "table",
      "title": "Variants — gene BRCA1",
      "columns": [{ "key": "variant", "label": "Variant" }, { "key": "gene", "label": "Gene" }],
      "rows": [
        {
          // A cell is either a bare string…
          "gene": "BRCA1",
          // …or a tagged object. Only `link` exists today.
          "variant": {
            "kind": "link",
            "label": "chr17:g.43045703C>T",
            "href": "https://portal.example.org/variants/entity/1234"
          }
        }
      ]
    }
  ]
}
```

Bare-string cells are exactly what the portal POC already accepts
(`rows: Record<string, string>[]`), so `link` is the only new thing to learn.
The envelope is also published as the tool's `output_schema`, so clients can
see the contract without reading this file.

`link_cell(label, href)` returns a plain string when `href` is falsy, so the
table degrades to text automatically when `PORTAL_BASE_URL` is unset.

## Portal side — what needs to change

The POC (`feat/sjra-1740-poc-chat`) types cells as `Record<string, string>`, so
it needs a typed cell before links work. Three small edits:

**1. `src/types.ts`** — add the cell union:

```ts
export type LinkCell = { kind: 'link'; label: string; href: string };
export type Cell = string | LinkCell;

export type TableBlock = {
  type: 'table';
  title?: string;
  columns: TableColumn[];
  rows: Record<string, Cell>[];   // was Record<string, string>[]
};
```

**2. `src/engine/wire.ts`** — widen the row schema (the rest of `parseBlocks`
is unchanged; unknown cell shapes still drop the block rather than the reply):

```ts
const wireLinkCell = z.object({
  kind: z.literal('link'),
  label: z.string(),
  href: z.string().url(),
});
const wireCell = z.union([z.string(), wireLinkCell]);

const wireTableBlock = z.object({
  type: z.literal('table'),
  title: z.string().optional(),
  columns: z.array(z.object({ key: z.string(), label: z.string() })),
  rows: z.array(z.record(z.string(), wireCell)),
});
```

**3. `src/blocks/table-block.tsx`** — render the cell:

```tsx
function CellValue({ value }: { value: Cell }) {
  if (typeof value === 'string') return <>{value}</>;
  return (
    <a
      href={value.href}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 text-primary hover:underline"
    >
      {value.label}
      <ExternalLink size={12} />
    </a>
  );
}
```

…then use `<CellValue value={row[column.key]} />` inside the existing
`<TableCell>`. Everything else in the panel is untouched — that is the point of
`BlockRenderer`.

The remaining piece is the `HttpEngine` that `engine.ts` already anticipates: it
calls the MCP tool, reads `structuredContent` off the tool result, and hands it
to `parseBlocks()`. Nothing else in the UI changes.

> Same-origin note: `href` is an absolute URL because the MCP server has no idea
> where the portal is mounted. If the panel is running on that same origin you
> can strip the origin and use react-router's `<Link>` for a client-side
> navigation instead of a full page load.

## Why not an mcp-ui resource

The first cut returned an [mcp-ui](https://mcpui.dev/) `ui://` resource carrying
self-contained HTML, which hosts show in a sandboxed iframe. It worked, and we
removed it. Recorded here so nobody re-adds it without new information;
`git log -- radiant_mcp/ui.py` has the implementation.

Verified against the **running LibreChat v0.8.7 image**, not the upstream repo:

1. **Links inside the frame can never open.** It is sandboxed `allow-scripts`
   only — no `allow-popups`, so `target="_blank"` is refused — and
   `handleUIAction` early-returns on anything that is not
   `intent`/`tool`/`prompt`, so the mcp-ui `link` action is dropped. Both routes
   closed.
2. **The default behaviour is actively broken.** Left alone the browser
   navigates the *frame* to the target, so the portal loads at origin `null`
   inside the sandbox and every script it fetches dies on
   `No 'Access-Control-Allow-Origin' header` — a half-rendered app in a small box.
3. **Upstream is not fixing it.** `v0.8.8-rc1` and current `main` still list only
   `['intent','tool','prompt']`, with no issue or PR tracking `link`. `main`'s new
   `Renderer.tsx` explicitly does `delete htmlProps.sandboxPermissions` and pins
   `supportedContentTypes={['rawHtml']}` — the sandbox is being tightened.
4. **It competed with the Markdown.** LibreChat hands the model a `\ui{id}`
   marker to place, so the same table risks being shown twice.

Markdown links are clickable in every client, and the portal panel renders real
components from `structuredContent`, so the iframe bought styling in exchange
for a dead link column.

Revisiting it needs an upstream change: `'link'` added to `supportedTypes` (a
small PR — push it into the array and `window.open` the payload), or
`allow-popups` in the sandbox.

### One fixed trap, for the record

Our resource used the mcp-ui SDK mime type `text/html;profile=mcp-app`.
`@mcp-ui/client` 5.7.0 — which v0.8.7 pins — compares with exact equality, so it
rendered a red *"Unsupported resource type."* Plain `text/html` worked.
[PR #14868](https://github.com/danny-avila/LibreChat/pull/14868) (merged
2026-08-15, after v0.8.8-rc1) normalizes the mime type, so the profile form will
work in some future release.

## Testing

Two layers. The first is the one to run while iterating; the second proves the
whole chain.

**Offline (seconds, no Docker).** Registers the tool on the real upstream
FastMCP instance with a fake DB client swapped in exactly the way `__main__.py`
swaps the real one, then asserts on SQL binding, null/array formatting, the
input-schema shape, and the two-part result:

```bash
.venv/bin/python tests/unit/test_generative_ui.py
```

**End-to-end (Keycloak → JWT → StarRocks).** `init-starrocks.sh` seeds a minimal
`snv__variant` fixture (3 BRCA1 rows + 1 BRCA2 row, one with NULLs), and the
integration suite asserts the Markdown, the deep link, `structuredContent`, the
absence of any `ui://` resource, and that the gene filter excludes BRCA2:

```bash
docker compose up -d --build
docker compose --profile test run --rm test-runner
```

**Against QA, in LibreChat.** Start the StarRocks tunnel
(`qlin-qa-infra/scripts/starrocks-tunnel.sh`), run `./scripts/run-local-qa.sh`,
and ask LibreChat's `radiant-local` agent for *"the BRCA1 variants"*. The server
runs on the host rather than in a container because the SSM tunnel binds
`127.0.0.1` — see the header comment in that script.

## Adding another rendered tool

1. Build blocks with the `radiant_mcp.blocks` helpers.
2. `return B.tool_result(blocks)`.
3. Register it from `register()` in your module, called from `__main__.py`.

Adding a **new block type** (chart, badge, …) means touching three places: the
builder in `blocks.py`, the Markdown branch in `blocks.to_markdown`, and the
portal's `wire.ts` + `BlockRenderer`.
Unknown block types are skipped rather than fatal on both sides, so a server
that runs ahead of the portal degrades instead of breaking.

## Why not MCP Apps / fastmcp Prefab?

fastmcp 3.x ships `prefab_ui` (declarative components over the MCP Apps
extension, SEP-1865), and it is the direction the ecosystem is heading. We are
not using it yet: LibreChat only renders *legacy* mcp-ui resources
([#10641](https://github.com/danny-avila/LibreChat/issues/10641) tracks MCP Apps
support) and the portal panel renders our own components anyway. If that
changes, `blocks.tool_result` is the only place that has to grow a new
representation — the block contract and the portal renderer stay as they are.
