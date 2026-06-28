import type { NextRequest } from "next/server";

/**
 * Proxy route for the ce-services cost HITL session API (port 8101).
 *
 * The browser calls ``/api/cost/<rest>`` (same-origin, no CORS) and this
 * route forwards to ``<CE_SERVICES_BASE_URL>/cost/<rest>`` — e.g.
 * ``/api/cost/session/start`` → ``http://127.0.0.1:8101/cost/session/start``.
 * Mirrors the gateway proxy in ``api/memory`` but targets the separate
 * ce-services process (kept decoupled from the deer-flow gateway on 8001).
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

  return new Response(await response.arrayBuffer(), {
    status: response.status,
    headers: response.headers,
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
