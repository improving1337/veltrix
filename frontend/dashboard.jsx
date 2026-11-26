import React, {useEffect, useState} from 'react'
import {createRoot} from 'react-dom/client'
import './dashboard.css'

// Card slots adapted from Dashral. See DASHRAL-LICENSE.
function Card({title, children, action, className = ''}) {
  return <section data-slot="card" className={'dash-card ' + className}>
    {title && <header data-slot="card-header"><h2 data-slot="card-title">{title}</h2>{action}</header>}
    <div data-slot="card-content">{children}</div>
  </section>
}

const number = new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2})
const date = new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', hour: '2-digit', minute: '2-digit'})
const day = new Intl.DateTimeFormat('ru-RU', {timeZone: 'Europe/Moscow', day: 'numeric', month: 'short'})
const fmt = value => value == null ? '—' : number.format(value)
const services = {views: 'Просмотры', reactions: 'Реакции', subscribe: 'Подписки', channels: 'Подписки', starts: 'Запуски бота', btn: 'Нажатия', unsub: 'Отписки', reposts: 'Репосты', polls: 'Опросы', join: 'Подписки', vote: 'Опросы', multi: 'Мульти', repost: 'Репосты', react: 'Реакции'}

function navigate(tab, filter) {
  window.dispatchEvent(new CustomEvent('veltrix:navigate', {detail: {tab, filter}}))
}

function useOverview(poll = false) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    async function refresh() {
      try {
        const response = await fetch('/api/overview', {signal: controller.signal})
        if (!response.ok) throw Error()
        setData(await response.json())
        setError(false)
      } catch {
        if (!controller.signal.aborted) setError(true)
      }
    }
    refresh()
    const timer = poll && setInterval(() => { if (!document.hidden) refresh() }, 30000)
    return () => { clearInterval(timer); controller.abort() }
  }, [poll])
  return {data, error}
}

function Metric({title, value, tone, onClick}) {
  const Tag = onClick ? 'button' : 'div'
  return <Tag className={'dash-card dash-metric ' + (tone || '')} onClick={onClick}>
    <span>{title}</span><strong>{fmt(value)}</strong>
  </Tag>
}

function Chart({series, lines, range}) {
  const cutoff = Date.now() / 1000 - range * 86400
  const samples = series.filter(point => point.at >= cutoff)
  const values = samples.flatMap(point => lines.map(line => point[line.key])).filter(Number.isFinite)
  if (!values.length) return <p className="dash-empty">История появится после первого замера.</p>
  const top = Math.max(1, ...values)
  const first = samples[0].at
  const span = Math.max(1, samples.at(-1).at - first)
  const x = at => 64 + (at - first) / span * 548
  const y = value => 193 - value / top * 165
  const ticks = span <= 1 ? [first] : [first, first + span / 2, first + span]
  return <>
    <svg viewBox="0 0 640 230" className="dash-chart" role="img" aria-label={lines.map(line => line.label).join(', ')}>
      {[0, .25, .5, .75, 1].map(tick => <g key={tick}>
        <line x1="64" x2="612" y1={y(tick * top)} y2={y(tick * top)} />
        <text x="55" y={y(tick * top) + 4} textAnchor="end">{fmt(tick * top)}</text>
      </g>)}
      {ticks.map((tick, index) => <text key={tick} x={x(tick)} y="218" textAnchor={['start','middle','end'][index]}>{span < 86400 ? date.format(tick * 1000) : day.format(tick * 1000)}</text>)}
      {lines.map(line => {
        let previous = false
        const path = samples.map(point => {
          if (!Number.isFinite(point[line.key])) { previous = false; return '' }
          const command = previous ? 'L' : 'M'
          previous = true
          return command + x(point.at) + ' ' + y(point[line.key])
        }).join(' ')
        const last = samples.findLast(point => Number.isFinite(point[line.key]))
        return <g key={line.key}><path stroke={line.color} d={path} />{last && <circle cx={x(last.at)} cy={y(last[line.key])} r="4" fill={line.color}/>}</g>
      })}
    </svg>
    <div className="dash-legend">{lines.map(line => <span key={line.key}><i style={{background: line.color}}/>{line.label}</span>)}</div>
  </>
}

function History({title, data, lines}) {
  const [range, setRange] = useState(7)
  return <Card title={title} action={<select aria-label="Период графика" value={range} onChange={event => setRange(Number(event.target.value))}><option value="1">24 часа</option><option value="7">7 дней</option><option value="14">14 дней</option></select>}>
    <Chart series={data?.series || []} range={range} lines={lines}/>
  </Card>
}

function Overview() {
  const {data, error} = useOverview(true)
  const totals = Object.entries(data?.services || {}).sort((a,b) => b[1] - a[1])
  const maximum = totals[0]?.[1] || 1
  return <div className="dash-page">
    <div className="dash-heading"><h1>Overview</h1><p>Аккаунты, услуги и состояние Автопилота</p></div>
    {error && <p className="dash-error" role="alert">Не удалось обновить сводку.</p>}
    <div className="dash-metrics">
      <Metric title="Аккаунты" value={data?.accounts} onClick={() => navigate('accounts','all')}/>
      <Metric title="Активные" value={data?.active} tone="dash-green" onClick={() => navigate('accounts','active')}/>
      <Metric title="Замороженные" value={data?.frozen} tone="dash-red" onClick={() => navigate('accounts','frozen')}/>
      <Metric title="Действий выполнено" value={data?.completed} onClick={() => navigate('orders')}/>
    </div>
    <div className="dash-split">
      <History title="Активные аккаунты" data={data} lines={[{key:'active',label:'Активные',color:'#74a5ff'}]}/>
      <Card title="Действия по услугам" action={<button className="ui-btn" onClick={() => navigate('orders')}>Услуги</button>}>
        {totals.length ? totals.map(([key,value]) => <div className="dash-bar" key={key}>
          <span>{services[key] || key}</span><strong>{fmt(value)}</strong><progress value={value} max={maximum}/>
        </div>) : <p className="dash-empty">Выполненных действий пока нет.</p>}
      </Card>
    </div>
    <div className="dash-split">
      <Card title="Последние посты Автопилота" action={<button className="ui-btn" onClick={() => navigate('automatic')}>Автопилот</button>}>
        {data?.posts.length ? <div className="dash-table"><table><thead><tr><th>Канал</th><th>Выполнено</th><th>По плану сейчас</th><th>Следующее</th></tr></thead>
          <tbody>{data.posts.map(post => <tr key={post.channel + '/' + post.id}><td>{post.channel.replace(/^https?:\/\/t\.me\//, '@')} · #{post.id}<small className="dash-post-mode">{post.mode === 'realistic' ? 'Реалистик' : 'Легаси'}</small></td><td>{post.done} / {post.target}</td><td>{post.due}</td><td>{post.next ? date.format(new Date(post.next)) : '—'}</td></tr>)}</tbody></table></div> : <p className="dash-empty">Постов с планом просмотров пока нет.</p>}
      </Card>
      <Card title="Заказы в работе">
        {data?.running.length ? data.running.map((order,index) => <div className="dash-bar" key={index}><span>{services[order.service] || order.title}</span><strong>{order.done} / {order.total}</strong><progress value={order.done} max={order.total || 1}/></div>) : <p className="dash-empty">Активных заказов сейчас нет.</p>}
      </Card>
    </div>
  </div>
}

function Balance() {
  const {data, error} = useOverview()
  return <div className="dash-balance">
    {error && <p className="dash-error">Не удалось загрузить историю баланса.</p>}
    <div className="dash-metrics"><Metric title="Общий баланс" value={data?.balance}/><Metric title="Наибольший баланс" value={data?.largest}/></div>
    <History title="История балансов" data={data} lines={[{key:'balance',label:'Общий баланс',color:'#74a5ff'},{key:'largest',label:'Наибольший баланс',color:'#42d5ab'}]}/>
  </div>
}

function Login() {
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  async function submit(event) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    setPending(true)
    setError('')
    try {
      const response = await fetch('/api/login', {method:'POST',headers:{'X-Panel':'1','Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(form))})
      if (response.ok) { location.replace('/'); return }
      setError(response.status === 429 ? 'Слишком много попыток. Попробуйте через минуту.' : 'Неверный логин или пароль.')
    } catch { setError('Панель недоступна. Попробуйте снова.') }
    setPending(false)
  }
  return <Card title="Вход в Veltrix" className="dash-login-card">
    <form onSubmit={submit}>
      <label>Логин<input name="username" autoComplete="username" required autoFocus/></label>
      <label>Пароль<input name="password" type="password" autoComplete="current-password" required/></label>
      {error && <p className="dash-error" role="alert">{error}</p>}
      <button disabled={pending}>{pending ? 'Вход…' : 'Войти'}</button>
    </form>
  </Card>
}

const overview = document.querySelector('#overview-root')
const login = document.querySelector('#login-root')
if (overview) createRoot(overview).render(<Overview/>)
if (login) createRoot(login).render(<Login/>)
window.VeltrixDashboard = {mountBalance: node => {
  const root = createRoot(node)
  root.render(<Balance/>)
  return () => root.unmount()
}}
