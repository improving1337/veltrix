// Split layout and gradient panel adapted from Dashral Classic's auth pages.
import {useEffect,useState} from 'react'
import {Activity, Bot, Check, Moon, Sun} from 'lucide-react'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {Input} from '@dashral/ui/components/input'
import {Sparkline} from '@dashral/ui/components/sparkline'
import {post,useAction} from './api'
import {Logo} from './shell'
import {Problem} from './ui'

function LoginPanel() {
  return <aside aria-label="О панели Veltrix"
    className="veltrix-auth-panel relative isolate hidden flex-col justify-between gap-12 overflow-hidden border-s p-12 lg:flex">
    <div aria-hidden className="veltrix-auth-grid absolute inset-0 -z-10"/>
    <ul className="grid gap-3 text-sm">
      {['Управление аккаунтами Telegram','Мониторинг каналов и публикаций','Графики и история выполнения'].map(title=>
        <li key={title} className="flex items-center gap-3">
          <span className="grid size-5 place-items-center rounded-full bg-primary/15 text-primary-text"><Check className="size-3"/></span>{title}
        </li>)}
    </ul>
    <div aria-hidden className="relative mx-auto w-full max-w-sm">
      <div className="rounded-2xl border bg-card/80 p-5 text-card-foreground shadow-lg backdrop-blur-sm">
        <div className="flex items-center gap-2 text-xs text-muted-foreground"><Activity className="size-4"/>Overview</div>
        <p className="mt-1 text-2xl font-semibold tracking-tight">Мои аккаунты</p>
        <Sparkline data={[25,25,30,30,33,36,36,39,40,42,48]} variant="area" showLast className="mt-3 h-16 text-primary"/>
      </div>
      <div className="absolute -end-6 -bottom-6 flex items-center gap-2.5 rounded-xl border bg-card px-3 py-2 text-xs text-card-foreground shadow-lg">
        <span className="grid size-7 place-items-center rounded-lg bg-primary/15 text-primary-text"><Bot className="size-3.5"/></span>Автопилот
      </div>
    </div>
    <p className="text-2xl font-medium tracking-tight">Veltrix</p>
  </aside>
}

export function Login() {
  const [dark,setDark]=useState(()=>localStorage.getItem('veltrix.color')!=='light')
  const action=useAction()
  useEffect(()=>{
    document.documentElement.classList.toggle('dark',dark)
    localStorage.setItem('veltrix.color',dark?'dark':'light')
  },[dark])
  function submit(event) {
    event.preventDefault()
    const data=Object.fromEntries(new FormData(event.currentTarget))
    action.run(async()=>{await post('/api/login',data);location.replace('/')})
  }
  return <div className="grid min-h-svh lg:grid-cols-2">
    <div className="flex min-h-svh flex-col">
      <header className="flex h-16 items-center justify-between px-4 md:px-6">
        <a href="/" className="flex items-center gap-2.5 font-semibold"><Logo/>Veltrix</a>
        <Button variant="ghost" size="icon-sm" onClick={()=>setDark(value=>!value)} aria-label={dark?'Светлая тема':'Тёмная тема'}>
          {dark?<Sun/>:<Moon/>}
        </Button>
      </header>
      <main className="flex flex-1 items-center justify-center px-4 pb-16">
        <div className="w-full max-w-sm *:data-[slot=card]:border-transparent *:data-[slot=card]:bg-transparent *:data-[slot=card]:shadow-none">
          <Card title="Вход в панель" description="Войдите с учётной записью Veltrix."
            classNames={{header:'text-center',title:'text-xl',body:'space-y-5'}}>
            <form className="space-y-4" onSubmit={submit}>
              <Input name="username" label="Логин" autoComplete="username" required autoFocus/>
              <Input name="password" label="Пароль" type="password" autoComplete="current-password" required/>
              <Problem message={action.error}/>
              <Button type="submit" className="w-full" disabled={action.busy}>{action.busy?'Вход…':'Войти'}</Button>
            </form>
          </Card>
        </div>
      </main>
    </div>
    <LoginPanel/>
  </div>
}
