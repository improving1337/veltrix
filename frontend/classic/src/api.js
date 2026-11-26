import {useCallback, useEffect, useRef, useState} from 'react'

export async function api(path, {method = 'GET', data, body, signal} = {}) {
  const headers = {'X-Panel': '1'}
  if (data !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(data)
  }
  const response = await fetch(path, {method, headers, body, signal})
  if (response.status === 401 && path !== '/api/login') {
    location.replace('/login')
    throw new Error('Войдите в панель')
  }
  const text = await response.text()
  let result
  try { result = JSON.parse(text) } catch { result = null }
  if (!response.ok) throw new Error(result?.error || result?.message || text || response.statusText)
  return result
}

export const post = (path, data = {}) => api(path, {method:'POST', data})
export const del = path => api(path, {method:'DELETE'})
export const patch = (path, data) => api(path, {method:'PATCH', data})
export function upload(path, file, fields = {}, field = 'file') {
  const body = new FormData()
  body.append(field, file)
  for (const [key,value] of Object.entries(fields)) if (value != null) body.append(key, value)
  return api(path, {method:'POST', body})
}

export function useData(path, interval = 0) {
  const [snapshot, setSnapshot] = useState({path, data:null, error:''})
  const generation = useRef(0)
  const reload = useCallback(async () => {
    if (!path) return
    const id = ++generation.current
    try {
      const data = await api(path)
      if (id === generation.current) setSnapshot({path, data, error:''})
      return data
    } catch (error) {
      if (id === generation.current) setSnapshot(s => ({path, data:s.path === path ? s.data : null, error:error.message}))
    }
  }, [path])
  useEffect(() => {
    reload()
    const timer = interval && setInterval(() => {if (!document.hidden) reload()}, interval)
    return () => {clearInterval(timer); ++generation.current}
  }, [reload, interval])
  return {...(snapshot.path === path ? snapshot : {data:null, error:''}), reload}
}

export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const running = useRef(false)
  async function run(action) {
    if (running.current) return
    running.current = true
    setBusy(true)
    setError('')
    try { return await action() } catch (error) { setError(error.message) }
    finally { running.current = false; setBusy(false) }
  }
  return {busy, error, run, setError}
}

export const flags = {general:'Общий', autopilot:'Автопилот', public:'Публичный'}
export const fullName = a => [a?.first_name,a?.last_name].filter(Boolean).join(' ') || a?.name || '—'
export const url = encodeURIComponent
export const accountPath = name => '/api/accounts/' + url(name)
export const connected = items => items.filter(account => account.connected && !account.frozen)
const numbers = new Intl.NumberFormat('ru-RU', {maximumFractionDigits:2})
export const num = n => n == null || !Number.isFinite(+n) ? '—' : numbers.format(+n)
export function timestamp(value) {
  const time = typeof value === 'number' ? new Date(value * 1000) : new Date(value)
  return value && Number.isFinite(time.getTime()) ? time : null
}
export function moscow(value, options = {dateStyle:'short',timeStyle:'short'}) {
  const time = timestamp(value)
  return time ? new Intl.DateTimeFormat('ru-RU',{...options,timeZone:'Europe/Moscow'}).format(time) : '—'
}
