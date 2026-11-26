import {useState} from 'react'
import {Save} from 'lucide-react'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {Input} from '@dashral/ui/components/input'
import {post,useAction,useData} from './api'
import {Check,Loading,Picker,Problem} from './ui'
import {PageHeader} from './page-header'
import {labels} from './shell'

export function Settings({settings,reload}) {
  return <>
    <PageHeader title="Настройки" description="Навигация, параметры Автопилота и отправка почты."/>
    {settings?<PanelSettings data={settings} reload={reload}/>:<Loading/>}
    <GmailSettings/>
  </>
}

function PanelSettings({data,reload}) {
  const [form,setForm]=useState(data)
  const [saved,setSaved]=useState(false)
  const action=useAction()
  const set=(key,value)=>{setSaved(false);setForm(current=>({...current,[key]:value}))}
  return <Card title="Панель и Автопилот">
    <form className="grid gap-4" onSubmit={event=>{
      event.preventDefault()
      action.run(async()=>{
        await post('/api/panel-settings',{...form,view_spread:Number(form.view_spread),reaction_spread:Number(form.reaction_spread),search_workers:Number(form.search_workers)})
        await reload();setSaved(true)
      })
    }}>
      <div><p className="mb-2 text-sm font-medium">Видимые вкладки</p>
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">{Object.entries(labels).filter(([key])=>!['overview','settings'].includes(key)).map(([key,label])=>
          <Check key={key} checked={!form.hidden_tabs.includes(key)} onChange={checked=>set('hidden_tabs',checked?form.hidden_tabs.filter(item=>item!==key):[...form.hidden_tabs,key])}>{label}</Check>)}</div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {[['view_spread','Разброс просмотров (±%)',0,100],['reaction_spread','Разброс реакций ниже лимита (%)',0,100],['search_workers','Аккаунтов для поиска профилей',1],['profile_group','Группа для поиска профилей']].map(([key,label,min,max])=>
          <Input key={key} label={label} type={min==null?'text':'number'} min={min} max={max} value={form[key]} onChange={event=>set(key,event.target.value)} required={min!=null}/>)}
      </div>
      <Check checked={form.autopilot_for_new_accounts} onChange={value=>set('autopilot_for_new_accounts',value)}>Назначать «Автопилот» новым аккаунтам</Check>
      <Problem message={action.error}/>
      {saved&&<p className="text-sm text-success-text" role="status">Настройки сохранены.</p>}
      <div><Button type="submit" disabled={action.busy}><Save/>Сохранить</Button></div>
    </form>
  </Card>
}

function GmailSettings() {
  const query=useData('/api/vpn/gmail-settings')
  return <Card title="Отправка через Gmail" description="Изменения применяются сразу.">
    <Problem message={query.error}/>
    {query.data?<GmailForm initial={query.data}/>:!query.error&&<Loading/>}
  </Card>
}
const gmailSecrets=[['client_id','OAuth Client ID'],['client_secret','OAuth Client Secret'],['refresh_token','OAuth Refresh Token']]
function GmailForm({initial}) {
  const [form,setForm]=useState({transport:initial.transport,email:initial.email,smtp_port:initial.smtp_port})
  const [configured,setConfigured]=useState(initial.configured||{})
  const [saved,setSaved]=useState(false)
  const action=useAction()
  const set=(key,value)=>{setSaved(false);setForm(current=>({...current,[key]:value}))}
  const keys=form.transport==='api'?gmailSecrets:[['app_password','Пароль приложения']]
  return <form className="mt-2 grid gap-4" onSubmit={event=>{
    event.preventDefault()
    action.run(async()=>{
      const result=await post('/api/vpn/gmail-settings',form)
      setConfigured(result.configured);setSaved(true)
      setForm({transport:result.transport,email:result.email,smtp_port:result.smtp_port})
    })
  }}>
    <div className="grid gap-3 sm:grid-cols-2">
      <Picker label="Отправка" items={[[ 'api','Gmail API'],['smtp','SMTP']]} value={form.transport} onValueChange={value=>set('transport',value)}/>
      <Input label="Адрес Gmail" type="email" value={form.email||''} onChange={event=>set('email',event.target.value)} required/>
      {form.transport==='smtp'&&<Picker label="SMTP-порт" items={[[465,'465'],[587,'587']]} value={form.smtp_port} onValueChange={value=>set('smtp_port',value)}/>}
    </div>
    <div className="grid gap-3 sm:grid-cols-2">
      {keys.map(([key,label])=><Input key={key} label={label} type="password" placeholder={configured[key]?'Сохранён':'Введите значение'} value={form[key]||''} onChange={event=>set(key,event.target.value)} autoComplete="off"/>)}
    </div>
    <Problem message={action.error}/>
    {saved&&<p className="text-sm text-success-text" role="status">Настройки Gmail сохранены.</p>}
    <div><Button type="submit" disabled={action.busy}><Save/>Сохранить Gmail</Button></div>
  </form>
}
