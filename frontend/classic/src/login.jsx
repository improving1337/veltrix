import {LogIn} from 'lucide-react'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {Input} from '@dashral/ui/components/input'
import {post,useAction} from './api'
import {Logo} from './shell'
import {Problem} from './ui'

export function Login() {
  const action=useAction()
  function submit(event) {
    event.preventDefault()
    const data=Object.fromEntries(new FormData(event.currentTarget))
    action.run(async()=>{await post('/api/login',data);location.replace('/')})
  }
  return <main className="grid min-h-svh place-items-center bg-muted/30 p-4">
    <div className="w-full max-w-sm">
      <div className="mb-8 flex items-center justify-center gap-2.5 text-lg font-semibold"><Logo/>Veltrix</div>
      <Card title="Вход в панель" description="Войдите с учётной записью Veltrix.">
        <form className="grid gap-4" onSubmit={submit}>
          <Input name="username" label="Логин" autoComplete="username" required autoFocus/>
          <Input name="password" label="Пароль" type="password" autoComplete="current-password" required/>
          <Problem message={action.error}/>
          <Button type="submit" disabled={action.busy}><LogIn/>{action.busy?'Вход…':'Войти'}</Button>
        </form>
      </Card>
    </div>
  </main>
}
