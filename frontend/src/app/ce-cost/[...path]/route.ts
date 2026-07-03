import type { NextRequest } from "next/server";

/**
 * Proxy route for the ce-services cost HITL session API (port 8101).
 *
 * The browser calls ``/ce-cost/<rest>`` (same-origin, no CORS) and this route
 * forwards to ``<CE_SERVICES_BASE_URL>/cost/<rest>`` — e.g.
 * ``/ce-cost/session/start`` → ``http://127.0.0.1:8101/cost/session/start``.
 *
 * NOTE: deliberately **not** under ``/api/*``. next.config rewrites a catch-all
 * ``/api/:path*`` to the gateway (:8001), and afterFiles rewrites beat dynamic
 * (catch-all) route handlers — so an ``/api/cost`` route would be shadowed and
 * land on the gateway (401/404). ``/ce-cost`` is outside that rewrite, so Next
 * serves it directly here. Keep this path off ``/api``.
 */
const CE_SERVICES_BASE_URL =
  process.env.NEXT_PUBLIC_CE_SERVICES_BASE_URL ?? "http://127.0.0.1:8101";

async function proxyRequest(request: NextRequest, path: string[]) {
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  const search = new URL(request.url).search;
  const target = new URL(`/cost/${path.join("/")}${search}`, CE_SERVICES_BASE_URL);

  const hasBody = !["GET", "HEAD"].includes(request.method);
  const response = await fetch(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
  });

  // Stream the body straight through (don't buffer) so SSE (text/event-stream)
  // reaches the browser incrementally — the cost HITL step timeline relies on it.
  // Drop hop-by-hop/length headers that break a streamed passthrough.
  const outHeaders = new Headers(response.headers);
  outHeaders.delete("content-length");
  outHeaders.delete("content-encoding");
  outHeaders.delete("transfer-encoding");
  return new Response(response.body, {
    status: response.status,
    headers: outHeaders,
  });
}

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyRequest(request, (await params).path);
}

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  return proxyRequest(request, (await params).path);
}
