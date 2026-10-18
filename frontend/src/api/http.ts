export const BASE = import.meta.env.VITE_API_URL ?? `${import.meta.env.BASE_URL}api`

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }

  /** The contract exists but its owner has not filled it in yet (501). */
  get notImplemented(): boolean {
    return this.status === 501
  }

  /**
   * The backend is not up, or CORS blocked us. Vite's proxy answers 502 with no
   * `{code, message}` body when nothing listens behind it; a backend 502 carries a code.
   */
  get unreachable(): boolean {
    return this.status === 0 || (this.status >= 502 && this.status <= 504 && this.code === 'http_error')
  }
}

let userId: number | null = null
let onIdentityRejected: (() => void) | null = null

export function setUserId(id: number | null) {
  userId = id
}

/** Called when the identity is missing or invalid: 401 or a 422 on `x-user-id`. */
export function setOnIdentityRejected(callback: (() => void) | null) {
  onIdentityRejected = callback
}

function headers(extra?: HeadersInit): Headers {
  const result = new Headers(extra)
  if (userId != null) result.set('X-User-Id', String(userId))
  return result
}

async function fail(response: Response): Promise<never> {
  let code = 'http_error'
  let message = `${response.status} ${response.statusText}`
  // A forbidden action does not invalidate a selected operator's identity.
  let badIdentity = response.status === 401
  try {
    const body = await response.json()
    if (typeof body?.code === 'string') code = body.code
    if (typeof body?.message === 'string') message = body.message
    else if (typeof body?.detail === 'string') message = body.detail
    else if (Array.isArray(body?.detail)) {
      badIdentity ||= response.status === 422 && body.detail.some(
        (error: { loc?: string[] }) => error.loc?.includes('x-user-id'),
      )
      message = body.detail.map(
        (error: { loc?: string[]; msg?: string }) => `${error.loc?.join('.') ?? 'Configuration'}: ${error.msg ?? 'Invalid value'}`,
      ).join('; ')
    }
  } catch {
    // Body was not the `{code, message}` envelope. Keep the status text.
  }
  // The session asks for its identity again; the caller still gets the error to show.
  if (badIdentity) onIdentityRejected?.()
  throw new ApiError(response.status, code, message)
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, init)
  } catch (error) {
    throw new ApiError(0, 'unreachable', error instanceof Error ? error.message : 'Sin conexión')
  }
  if (!response.ok) await fail(response)
  return response
}

export async function get<T>(path: string): Promise<T> {
  const response = await send(path, { headers: headers() })
  return response.json() as Promise<T>
}

export async function getText(path: string): Promise<string> {
  const response = await send(path, { headers: headers() })
  return response.text()
}

export async function getBlob(path: string): Promise<Blob> {
  const response = await send(path, { headers: headers() })
  return response.blob()
}

export async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await send(path, {
    method: 'POST',
    headers: headers(body === undefined ? undefined : { 'Content-Type': 'application/json' }),
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return response.status === 204 ? (undefined as T) : (response.json() as Promise<T>)
}

export async function put<T>(path: string, body: unknown): Promise<T> {
  const response = await send(path, {
    method: 'PUT',
    headers: headers({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(body),
  })
  return response.json() as Promise<T>
}

export async function del(path: string): Promise<void> {
  await send(path, { method: 'DELETE', headers: headers() })
}

export async function upload<T>(path: string, form: FormData): Promise<T> {
  // No Content-Type: the browser sets the multipart boundary.
  const response = await send(path, { method: 'POST', headers: headers(), body: form })
  return response.json() as Promise<T>
}

export function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}
