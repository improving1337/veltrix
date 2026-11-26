import {useEffect,useEffectEvent,useRef,useState} from 'react'
import {ClipboardPaste, FileUp, Phone, Shield} from 'lucide-react'
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
  const [quick,setQuick]=useState(false)
  const [clipboardHelp,setClipboardHelp]=useState('')
  const readRequest=useRef(null)
  const automaticReadBlocked=useRef(false)
  const lastSubmission=useRef('')

  const action=useAction()
  const security=useData('/api/security',15000)
  const confirm=useConfirm()
  function reset() {
    if(/^[0-9]{5}$/.test(lastSubmission.current))lastSubmission.current=''
    setStep('phone');setToken('');setCode('');setPassword('')
  }
  function submitLogin(pasted) {
    action.run(async()=>{
      setMessage('')
      if(step==='phone'){
        const response=await post('/api/deploy/phone',{phone:pasted??phone})
        setToken(response.token);setStep('code');setMessage('Код запрошен. Проверьте Telegram или SMS.')
        return
      }
      let response
      try {
        response=await post('/api/deploy/verify',step==='code'?{token,code:pasted??code}:{token,password})
      } finally {
        if(pasted!=null)setCode('')
        setPassword('')
      }
      if(response.stage==='password'){lastSubmission.current='';setStep('password');setMessage('Введите текущий пароль 2FA.');return}
      reset();setMessage(response.connected?'Аккаунт добавлен и подключён.':'Аккаунт добавлен. Статус подключения доступен в профилях.');await reloadAccounts()
    })
  }

  function useClipboardValue(text, explicit = false) {
    if(!quick||action.busy||step==='password')return false
    const clean=text.trim()
    const value=step==='phone'?clean.replace(/[\s()]/g,''):clean
    const valid=step==='phone'?/^\+[1-9][0-9]{6,14}$/.test(value):/^[0-9]{5}$/.test(value)
    if(!valid||(!explicit&&value===lastSubmission.current))return false
    lastSubmission.current=value
    if(step==='phone')setPhone(value)
    else setCode(value)
    submitLogin(value)
    return true
  }

  async function readClipboard(explicit = false) {
    if(!quick||action.busy||step==='password'||!document.hasFocus()||readRequest.current||(!explicit&&automaticReadBlocked.current))return
    if(!navigator.clipboard?.readText) {
      automaticReadBlocked.current=true
      setClipboardHelp('Чтение буфера недоступно. Используйте Ctrl+V или ⌘V.')
      return
    }
    const request={}
    readRequest.current=request
    try {
      const text=await navigator.clipboard.readText()
      if(readRequest.current!==request||!document.hasFocus())return
      automaticReadBlocked.current=false
      setClipboardHelp('')
      if(!useClipboardValue(text,explicit)&&explicit) {
        setClipboardHelp(step==='phone'?'Скопируйте номер с + и кодом страны.':'Скопируйте пятизначный код.')
      }
    } catch {
      if(readRequest.current===request) {
        automaticReadBlocked.current=true
        setClipboardHelp('Браузер не разрешил чтение буфера. Используйте Ctrl+V или ⌘V.')
      }
    } finally {
      if(readRequest.current===request)readRequest.current=null
    }
  }

  const readOnReturn=useEffectEvent(()=>{void readClipboard()})
  const handlePaste=useEffectEvent(event=>{
    if(event.defaultPrevented)return
    const target=event.target
    if(target instanceof HTMLElement&&target.closest('input, textarea, select, [contenteditable]')&&
      !(target instanceof HTMLInputElement&&target.name===step))return
    if(useClipboardValue(event.clipboardData?.getData('text/plain')||'',true)){
      event.preventDefault()
      setClipboardHelp('')
    }
  })

  useEffect(()=>{
    if(!quick||step==='password')return
    const onReturn=()=>{if(document.visibilityState==='visible')readOnReturn()}
    const onPaste=event=>handlePaste(event)
    window.addEventListener('focus',onReturn)
    document.addEventListener('visibilitychange',onReturn)
    document.addEventListener('paste',onPaste)
    return ()=>{
      readRequest.current=null
      window.removeEventListener('focus',onReturn)
      document.removeEventListener('visibilitychange',onReturn)
      document.removeEventListener('paste',onPaste)
    }
  },[quick,step,token,action.busy])

  return <>
    <PageHeader title="Deploy" description="Подключение аккаунтов Telegram."/>
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Card title={<span className="flex items-center gap-2"><Phone className="size-4"/>Вход по номеру</span>} description="Введите номер с кодом страны.">
        <form className="grid gap-4" onSubmit={event=>{event.preventDefault();submitLogin()}}>
          <Input label="Телефон" type="tel" name="phone" autoComplete="tel" placeholder="+79991234567" value={phone} onChange={event=>setPhone(event.target.value.replace(/[\s()]/g,''))} required disabled={step!=='phone'}/>
          {step==='code'&&<Input label="Код из Telegram" name="code" value={code} onChange={event=>setCode(event.target.value)} autoComplete="one-time-code" inputMode="numeric" required autoFocus/>}
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
          <div className="grid gap-2 border-b border-divider pb-3">
            <Check checked={quick} onChange={enabled=>{
              lastSubmission.current=''
              readRequest.current=null
              automaticReadBlocked.current=false
              setClipboardHelp('')
              setQuick(enabled)
            }}>Быстрое добавление</Check>
            {quick&&<>
              <p className="text-sm text-muted-foreground" role="status">
                {step==='password'?'Пауза. Введите пароль 2FA вручную.':step==='code'?
                  'Скопируйте пятизначный код и вернитесь в Deploy. Код отправится для текущего входа.':
                  'Скопируйте номер с + и вернитесь в Deploy. Панель запросит код.'}
              </p>
              {step!=='password'&&<div>
                <Button type="button" size="sm" variant="outline" disabled={action.busy} onClick={()=>readClipboard(true)}><ClipboardPaste/>Вставить из буфера</Button>
              </div>}
              {clipboardHelp&&step!=='password'&&<p className="text-sm text-muted-foreground" role="status">{clipboardHelp}</p>}
            </>}
          </div>
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
