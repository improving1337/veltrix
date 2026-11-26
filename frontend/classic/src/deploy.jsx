import {useState} from 'react'
import {FileUp, Phone, Shield} from 'lucide-react'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {Input} from '@dashral/ui/components/input'
import SortableTable from './tables'
import {post,upload,useAction,useData} from './api'
import {Check,FormActions,Problem,TextArea,useConfirm} from './ui'
import {PageHeader} from './page-header'

export function Deploy({settings,reloadSettings,reloadAccounts}) {
  const [phone,setPhone]=useState('')
  const [step,setStep]=useState('phone')
  const [token,setToken]=useState('')
  const [code,setCode]=useState('')
  const [password,setPassword]=useState('')
  const [message,setMessage]=useState('')
  const action=useAction()
  const security=useData('/api/security',15000)
  const confirm=useConfirm()
  function reset() {setStep('phone');setToken('');setCode('');setPassword('')}
  function signIn(event) {
    event.preventDefault()
    action.run(async()=>{
      setMessage('')
      if(step==='phone'){
        const response=await post('/api/deploy/phone',{phone})
        setToken(response.token);setStep('code');setMessage('Код запрошен. Проверьте Telegram или SMS.')
        return
      }
      const response=await post('/api/deploy/verify',step==='code'?{token,code}:{token,password})
      setPassword('')
      if(response.stage==='password'){setStep('password');setMessage('Введите текущий пароль 2FA.');return}
      reset();setMessage(response.connected?'Аккаунт добавлен и подключён.':'Аккаунт добавлен. Статус подключения доступен в профилях.');await reloadAccounts()
    })
  }
  return <>
    <PageHeader title="Deploy" description="Подключение аккаунтов Telegram."/>
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Card title={<span className="flex items-center gap-2"><Phone className="size-4"/>Вход по номеру</span>} description="Введите номер с кодом страны.">
        <form className="grid gap-4" onSubmit={signIn}>
          <Input label="Телефон" type="tel" name="phone" autoComplete="tel" placeholder="+79991234567" value={phone} onChange={event=>setPhone(event.target.value.replace(/[\s()]/g,''))} required disabled={step!=='phone'}/>
          {step==='code'&&<Input label="Код из Telegram" value={code} onChange={event=>setCode(event.target.value)} autoComplete="one-time-code" inputMode="numeric" required autoFocus/>}
          {step==='password'&&<Input label="Пароль 2FA" value={password} onChange={event=>setPassword(event.target.value)} type="password" autoComplete="current-password" required autoFocus/>}
          <FormActions>
            <Button type="submit" disabled={action.busy}>{step==='phone'?'Получить код':step==='code'?'Подтвердить код':'Войти с 2FA'}</Button>
            {step!=='phone'&&<Button type="button" variant="outline" disabled={action.busy} onClick={()=>action.run(async()=>{await post('/api/deploy/cancel',{token});reset();setMessage('Вход отменён')})}>Отменить</Button>}
          </FormActions>
          <Problem message={action.error}/>
          {message&&<p className="text-sm text-muted-foreground" role="status">{message}</p>}
        </form>
      </Card>
      <Card title={<span className="flex items-center gap-2"><Shield className="size-4"/>Настройки подключения</span>}>
        <div className="grid gap-3">
          <Check checked={!!settings?.autopilot_for_new_accounts} disabled={!settings||action.busy} onChange={value=>action.run(async()=>{
            await post('/api/panel-settings',{autopilot_for_new_accounts:value});await reloadSettings()
          })}>Ставить метку «Автопилот» новым аккаунтам</Check>
          <Check checked={!!security.data?.enabled} disabled={!security.data||action.busy} onChange={enabled=>action.run(async()=>{
            if(enabled&&!await confirm({title:'Включить автоматическую защиту?',text:'Для поддерживаемых аккаунтов панель настраивает 2FA и пытается оставить только собственную сессию.'}))return
            await post('/api/security',{enabled});await security.reload()
          })}>Автоматическая защита аккаунтов</Check>
          <Problem message={security.error}/>
        </div>
      </Card>
    </div>
    <DeployImport reloadAccounts={reloadAccounts}/>
  </>
}

function DeployImport({reloadAccounts}) {
  const [file,setFile]=useState(null)
  const [links,setLinks]=useState('')
  const [message,setMessage]=useState('')
  const job=useData('/api/upload/job',3000)
  const action=useAction()
  const state=job.data?.job
  const running=state?.status==='running'
  const columns=[{accessorKey:'url',header:'Ссылка'},{accessorKey:'state',header:'Статус'},{accessorKey:'note',header:'Результат'}]
  return <Card title="Дополнительные способы импорта">
    <div className="grid gap-3">
      <details className="rounded-lg border border-divider p-3">
        <summary className="cursor-pointer text-sm font-medium">Файл Telethon .session</summary>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <Input label="Файл сессии" type="file" accept=".session" onChange={event=>setFile(event.target.files[0])}/>
          <Button disabled={!file||action.busy} onClick={()=>action.run(async()=>{
            const result=await upload('/api/deploy/session',file);setMessage(result.connected?'Сессия добавлена и подключена.':'Сессия добавлена.');setFile(null);await reloadAccounts()
          })}><FileUp/>Импортировать</Button>
        </div>
      </details>
      <details className="rounded-lg border border-divider p-3">
        <summary className="cursor-pointer text-sm font-medium">Архивы по ссылкам</summary>
        <div className="mt-3 grid gap-3">
          <TextArea label="Ссылки, по одной на строку" rows={4} value={links} onChange={event=>setLinks(event.target.value)} placeholder="https://drive.google.com/file/d/.../view"/>
          <FormActions>
            <Button disabled={!links.trim()||running||action.busy} onClick={()=>action.run(async()=>{await post('/api/upload/start',{links});await job.reload()})}>Скачать и добавить</Button>
            {running&&<Button variant="outline" color="destructive" disabled={action.busy} onClick={()=>action.run(async()=>{await post('/api/upload/stop');await job.reload()})}>Остановить</Button>}
          </FormActions>
          {state?.items?.length>0&&<SortableTable columns={columns} data={state.items} getRowId={row=>row.url}/>}
        </div>
      </details>
      <Problem message={action.error}/>
      {message&&<p className="text-sm text-muted-foreground">{message}</p>}
    </div>
  </Card>
}
