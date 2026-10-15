export async function request(
  path,
  {
    body,
    signal,
    method = body === undefined ? "GET" : "POST",
    timeout = 45000,
  } = {},
) {
  const controller = AbortSignal.timeout(timeout);
  const response = await fetch(path, {
    method,
    signal: signal ? AbortSignal.any([signal, controller]) : controller,
    ...(body === undefined
      ? {}
      : {
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`HTTP ${response.status}：服务器未返回 JSON`);
  }
  if (!response.ok) {
    const detail = data.detail ?? data.error;
    const err = new Error(
      typeof detail === "string"
        ? detail
        : detail?.message ||
            JSON.stringify(detail ?? `HTTP ${response.status}`),
    );
    err.detail = detail;
    err.partial = !!detail?.partial;
    err.historyId = detail?.history_id;
    throw err;
  }
  return data;
}
