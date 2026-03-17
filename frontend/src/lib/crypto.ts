export async function signPrivateWs(apiKey: string, secret: string, timestamp: number) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, encoder.encode(`${apiKey}:${timestamp}`));
  return Array.from(new Uint8Array(signature))
    .map((item) => item.toString(16).padStart(2, "0"))
    .join("");
}
