export function basicCredentials(user: string, password: string): string {
  const bytes = new TextEncoder().encode(`${user}:${password}`)
  let binary = ''
  bytes.forEach((byte) => (binary += String.fromCharCode(byte)))
  return `Basic ${btoa(binary)}`
}

export async function adminApi<T>(
  credentials: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api/admin${path}`, {
    ...init,
    headers: {
      Authorization: credentials,
      'Content-Type': 'application/json',
      ...init.headers,
    },
  })
  if (!response.ok) {
    let message = `请求失败（${response.status}）`
    try {
      const body = await response.json()
      message = body.detail ?? message
    } catch {
      // Keep the status-based message for non-JSON failures.
    }
    throw new Error(message)
  }
  return response.json() as Promise<T>
}
