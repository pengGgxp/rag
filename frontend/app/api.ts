const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function request<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, options);
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `请求失败 (${response.status})`,
    );
  return data as T;
}
