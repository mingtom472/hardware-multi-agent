import { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Forward the raw request stream without Next's external-rewrite body cloning.
 * FastAPI enforces the actual byte limit and local storage quota while reading.
 * Do not replace request.body with arrayBuffer(), formData(), or base64 data.
 */
export async function POST(request: NextRequest) {
  const backend = (process.env.BACKEND_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
  const target = new URL(`${backend}/api/uploads`);
  target.search = request.nextUrl.search;
  const headers = new Headers();
  headers.set("content-type", request.headers.get("content-type") ?? "application/octet-stream");
  const contentLength = request.headers.get("content-length");
  if (contentLength) headers.set("content-length", contentLength);
  try {
    const options: RequestInit & { duplex: "half" } = {
      method: "POST",
      headers,
      body: request.body,
      duplex: "half",
      signal: request.signal,
      cache: "no-store",
    };
    const upstream = await fetch(target, options);
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return Response.json({ detail: "上传代理无法连接后端，或上传已中断。请检查 FastAPI 服务。" }, { status: 502 });
  }
}
