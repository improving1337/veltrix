import {useState} from 'react'
import {ArrowDown, ArrowUp, Copy, ExternalLink, History, Mail, Plus, Shield, SlidersHorizontal, X} from 'lucide-react'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {DataTable} from '@dashral/ui/components/data-table'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {accountPath,connected,del,moscow,patch,post,useAction,useData,url} from './api'
import {AccountPicker,Empty,FormActions,Loading,Problem,useConfirm} from './ui'
import {PageHeader} from './page-header'

const recordLabel=record=>record.username?'@'+record.username:record.name
const vpnStatuses={active:['Активна','success'],ending:['Активна','warning'],expired:['Закончилась','destructive'],pending:['Ожидает ссылку','neutral']}
function timeLeft(seconds) {
  if(seconds==null)return '—'
  const minutes=Math.floor(seconds/60)
  return `${Math.floor(minutes/1440)} дн. ${Math.floor(minutes%1440/60)} ч. ${minutes%60} мин.`
}
async function copy(value) {
  if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(value);return}
  const field=document.createElement('textarea')
  field.value=value
  field.style.position='fixed'
  field.style.opacity='0'
  document.body.append(field)
  field.select()
  const copied=document.execCommand('copy')
  field.remove()
  if(!copied)throw new Error('Выделите ссылку и скопируйте вручную.')
}
export function Tools({accounts}) {
  const [mode,setMode]=useState('vpn')
  return <>
    <PageHeader title="VPN и Mini App" description="Ссылки подписок и запуск мини-приложений Telegram."/>
    <FormActions>
      <Button size="sm" variant={mode==='vpn'?'solid':'outline'} onClick={()=>setMode('vpn')}><Shield/>VPN</Button>
      <Button size="sm" variant={mode==='miniapp'?'solid':'outline'} onClick={()=>setMode('miniapp')}><ExternalLink/>Mini App</Button>
    </FormActions>
    {mode==='vpn'?<Vpn/>:<MiniApp accounts={accounts}/>}
  </>
}
function MiniApp({accounts}) {
  const [selected,setSelected]=useState('')
  const [link,setLink]=useState('')
  const action=useAction()
  return <Card title="Открыть Mini App" description="Выберите аккаунт, для которого Telegram выдаст ссылку.">
    <form className="grid gap-4" onSubmit={event=>{
      event.preventDefault()
      const page=window.open('about:blank','_blank')
      if(!page){action.setError('Разрешите всплывающие окна для панели');return}
      page.opener=null
      action.run(async()=>{
        try {
          const result=await post(accountPath(selected)+'/miniapp',{link:link.trim()})
          if(!/^https?:\/\//i.test(result.url||''))throw new Error('Telegram не вернул ссылку')
          page.location.replace(result.url)
        }catch(error){page.close();throw error}
      })
    }}>
      <AccountPicker items={connected(accounts)} value={selected} onChange={setSelected}/>
      <Input label="Ссылка на мини-приложение" placeholder="https://t.me/bot/app?startapp=param" value={link} onChange={event=>setLink(event.target.value)} required/>
      <Problem message={action.error}/>
      <div><Button type="submit" disabled={!selected||action.busy}><ExternalLink/>Открыть</Button></div>
    </form>
  </Card>
}

function Vpn() {
  const [result,setResult]=useState(null)
  const [mail,setMail]=useState(false)
  const [recipient,setRecipient]=useState('')
  const [note,setNote]=useState('')
  const [settings,setSettings]=useState(false)
  const [history,setHistory]=useState(false)
  const [record,setRecord]=useState(null)
  const state=useData(settings?'/api/vpn':null,settings?60000:0)
  const listing=useData(history?'/api/vpn/history':null)
  const action=useAction()
  const confirm=useConfirm()
  const columns=[
    {id:'remove',header:'',cell:({row})=><Button size="icon-sm" variant="ghost" color="destructive"
      aria-label={'Исключить '+recordLabel(row.original)} disabled={action.busy} onClick={()=>action.run(async()=>{
        if(!await confirm({title:'Исключить аккаунт из списка подписок?',text:recordLabel(row.original),danger:true}))return
        await del('/api/vpn/records/'+url(row.original.id));await state.reload()
      })}><X/></Button>},
    {id:'username',header:'Username аккаунта',accessorFn:recordLabel},
    {accessorKey:'status',header:'Статус',cell:({getValue})=>{
      const [label,color]=vpnStatuses[getValue()]||['—','neutral']
      return <Badge variant="soft" color={color}>{label}</Badge>
    }},
    {accessorKey:'remaining',header:'Сколько осталось',cell:({getValue})=>timeLeft(getValue())},
    {id:'edit',header:'',cell:({row})=>row.original.issued_at!=null&&<Button size="sm" variant="outline" onClick={()=>setRecord(row.original)}>Изменить дату</Button>},
  ]
  return <>
    <div className="mx-auto grid w-full max-w-2xl gap-4 pt-4 xl:pt-10">
      <Card title="Подписка VPN">
        {!result?<FormActions>
          <Button onClick={()=>action.run(async()=>{setNote('');setResult(await post('/api/vpn/issue'));if(history)listing.reload()})} disabled={action.busy}>{action.busy?'Получаю ссылку…':'Получить ссылку'}</Button>
          <Button variant="outline" onClick={()=>setSettings(value=>!value)}><SlidersHorizontal/>Настройки</Button>
          <Button variant="ghost" onClick={()=>setHistory(value=>!value)}><History/>Последние ссылки</Button>
        </FormActions>:<div className="grid gap-4">
          <Input label="Ссылка VPN" value={result.url} readOnly onFocus={event=>event.target.select()}/>
          <FormActions>
            <Button onClick={()=>action.run(async()=>{await copy(result.url);setNote('Ссылка скопирована')})}><Copy/>Скопировать</Button>
            <Button variant="outline" onClick={()=>{setResult(null);setMail(false);setNote('')}}>Скрыть</Button>
            <Button variant="outline" onClick={()=>setMail(value=>!value)}><Mail/>Получить на почту</Button>
          </FormActions>
          {mail&&<form className="flex flex-wrap items-end gap-2" onSubmit={event=>{
            event.preventDefault()
            action.run(async()=>{await post('/api/vpn/email',{recipient,receipt:result.receipt});setMail(false);setNote('Письмо отправлено')})
          }}>
            <Input type="email" label="Кому отправить" value={recipient} onChange={event=>setRecipient(event.target.value)} required/>
            <Button type="submit" disabled={action.busy}>Отправить</Button>
          </form>}
        </div>}
        <div className="mt-3"><Problem message={action.error}/>{note&&<p className="text-sm text-success-text" role="status">{note}</p>}</div>
      </Card>
      {history&&<Card title="Последние ссылки VPN">
        <Problem message={listing.error}/>
        {listing.data?.items?.length?<div className="grid gap-4">
          {listing.data.items.map(item=><div key={item.id} className="flex flex-wrap items-end gap-2">
            <div className="min-w-0 flex-1"><Input label={recordLabel(item)+' · '+moscow(item.issued_at)} value={item.url} readOnly onFocus={event=>event.target.select()}/></div>
            <Button variant="outline" size="icon" aria-label="Скопировать ссылку" onClick={()=>action.run(async()=>{await copy(item.url);setNote('Ссылка скопирована')})}><Copy/></Button>
          </div>)}
        </div>:listing.data?<Empty>Ссылок пока нет.</Empty>:!listing.error&&<Loading/>}
      </Card>}
    </div>
    {settings&&<>
      <Problem message={state.error}/>
      {state.data?<><VpnSettings settings={state.data.settings} refresh={state.reload}/>
        <Card title="Подписки аккаунтов" description="Срок считается от даты выдачи доступа.">
          <DataTable columns={columns} data={state.data.records} getRowId={record=>record.id} autoResetPage={false} emptyMessage="Выданных подписок нет"/>
        </Card></>:!state.error&&<Loading/>}
    </>}
    {record&&<EditDate key={record.id} record={record} close={()=>setRecord(null)} refresh={state.reload}/>}
  </>
}
function EditDate({record,close,refresh}) {
  const [date,setDate]=useState(new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'medium'})
    .format(new Date(record.issued_at*1000)).replace(' ','T'))
  const action=useAction()
  return <Dialog open onOpenChange={open=>{if(!open&&!action.busy)close()}} title="Дата получения подписки"
    description={recordLabel(record)} form={{onSubmit:event=>{
      event.preventDefault()
      action.run(async()=>{
        const stamp=Date.parse(date+'+03:00')/1000
        if(!Number.isFinite(stamp))throw new Error('Укажите дату и время по МСК')
        await patch('/api/vpn/records/'+url(record.id),{issued_at:stamp});await refresh();close()
      })
    }}} footer={<Button type="submit" disabled={action.busy}>Сохранить</Button>}>
    <div className="grid gap-3"><Input label="Дата и время (МСК)" step={1} type="datetime-local" value={date} onChange={event=>setDate(event.target.value)} required/><Problem message={action.error}/></div>
  </Dialog>
}
function VpnSettings({settings,refresh}) {
  const [form,setForm]=useState(settings)
  const [saved,setSaved]=useState(false)
  const action=useAction()
  const change=(key,value)=>{setSaved(false);setForm(previous=>({...previous,[key]:value}))}
  const changeStep=(index,field,value)=>change('steps',form.steps.map((step,i)=>i===index?{...step,[field]:value}:step))
  function move(index,next) {
    const steps=[...form.steps]
    ;[steps[index],steps[next]]=[steps[next],steps[index]]
    change('steps',steps)
  }
  return <Card title="Настройки VPN" description="Канал, бот и порядок действий для получения подписки.">
    <form className="grid gap-4" onSubmit={event=>{
      event.preventDefault()
      action.run(async()=>{
        await post('/api/vpn/settings',{...form,poll_delay:Number(form.poll_delay),attempts:Number(form.attempts),
          steps:form.steps.map(step=>({...step,position:Number(step.position),delay:Number(step.delay)}))})
        await refresh();setSaved(true)
      })
    }}>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {[['channel','Канал для подписки'],['bot','Бот'],['host','Домен ссылки'],['poll_delay','Пауза между проверками, с',1,30],['attempts','Количество попыток',1,10]].map(([key,label,min,max])=>
          <Input key={key} label={label} value={form[key]} type={min?'number':'text'} min={min} max={max} onChange={event=>change(key,event.target.value)} required={key!=='channel'}/>)}
      </div>
      <h3 className="text-sm font-medium">Кнопки бота по порядку</h3>
      {form.steps.map((step,index)=><div key={index} className="grid items-end gap-2 rounded-lg border border-divider p-3 sm:grid-cols-[2fr_1fr_1fr_auto]">
        <Input label="Текст кнопки" value={step.label} onChange={event=>changeStep(index,'label',event.target.value)}/>
        <Input label="Позиция" type="number" min={0} max={100} value={step.position} onChange={event=>changeStep(index,'position',event.target.value)} required/>
        <Input label="Пауза, с" type="number" min={0} max={30} value={step.delay} onChange={event=>changeStep(index,'delay',event.target.value)} required/>
        <FormActions>
          <Button type="button" size="icon-sm" variant="outline" disabled={index===0} onClick={()=>move(index,index-1)} aria-label="Поднять шаг"><ArrowUp/></Button>
          <Button type="button" size="icon-sm" variant="outline" disabled={index===form.steps.length-1} onClick={()=>move(index,index+1)} aria-label="Опустить шаг"><ArrowDown/></Button>
          <Button type="button" size="icon-sm" variant="ghost" color="destructive" disabled={form.steps.length===1} onClick={()=>change('steps',form.steps.filter((_,i)=>i!==index))} aria-label="Удалить шаг"><X/></Button>
        </FormActions>
      </div>)}
      <Problem message={action.error}/>{saved&&<p className="text-sm text-success-text" role="status">Настройки VPN сохранены.</p>}
      <FormActions><Button type="button" variant="outline" onClick={()=>change('steps',[...form.steps,{label:'',position:1,delay:1}])}><Plus/>Добавить шаг</Button><Button type="submit" disabled={action.busy}>Сохранить</Button></FormActions>
    </form>
  </Card>
}
