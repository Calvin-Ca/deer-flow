/**
 * Safe display formatter for the loosely-typed (``unknown``) values that come
 * back in cost session payloads (provenance fields, rollup numbers, gate
 * defaults). Avoids ``String(obj)`` producing ``[object Object]`` — objects are
 * JSON-stringified, primitives rendered directly, nullish shown as an em dash.
 */
export function displayValue(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean" || typeof v === "bigint") {
    return String(v);
  }
  return JSON.stringify(v);
}
