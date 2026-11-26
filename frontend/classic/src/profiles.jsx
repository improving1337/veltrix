import {useEffect,useMemo,useState} from 'react'
import {ArrowLeft, Camera, Plus, RefreshCw, Shield, Trash2, UserRoundCheck, Users} from 'lucide-react'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {DataTable,dataTableSelectColumn} from '@dashral/ui/components/data-table'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {MetricCard} from '@dashral/ui/components/metric-card'
import {accountPath,api,del,flags,fullName,moscow,post,upload,useAction,useData,url} from './api'
import {AccountAvatar,AccountState,Check,DownloadButton,Empty,FormActions,Loading,Picker,Problem,useConfirm} from './ui'
import {PageHeader} from './page-header'

const columns = [
  ['username','Юзернейм',a=>a.username?'@'+a.username:'—'],
  ['flag','Метка',a=>flags[a.flag]||flags.general],
  ['status','Статус',a=>a.frozen?'Заморожен':a.connected?'Активен':a.status||'Отключён'],
  ['phone','Телефон',a=>a.phone||'—'],
  ['birthday','День рождения',a=>a.profile_meta?.birthday||'—'],
  ['avatar_count','Аватарки',a=>a.profile_meta?.avatar_count??null],
  ['music_count','Музыка',a=>a.profile_meta?.music_count??null],
  ['session_count','Сессии',a=>a.security?.session_count??null],
  ['spam_status','Спамбан',a=>a.spam_status?.label||'Не проверен'],
  ['last_avatar','Смена аватарки',a=>moscow(a.profile_meta?.last_avatar_change,{dateStyle:'short'})],
]
const statuses=[['all','Все статусы'],['active','Активные'],['frozen','Замороженные'],['offline','Отключённые']]
const accountFlags=Object.entries(flags)
function useWide() {
  const [wide,setWide]=useState(()=>matchMedia('(min-width: 768px)').matches)
  useEffect(()=>{
    const media=matchMedia('(min-width: 768px)')
    const change=event=>setWide(event.matches)
    media.addEventListener('change',change)
    return ()=>media.removeEventListener('change',change)
  },[])
  return wide
}

function Bulk({table,reload}) {
  const rows=table.getSelectedRowModel().flatRows.map(row=>row.original)
  const exportRows=rows.length?rows:table.getSortedRowModel().flatRows.map(row=>row.original)
  const visible=new Set(table.getVisibleLeafColumns().map(column=>column.id))
  const selectedCols=columns.filter(([id])=>visible.has(id))
  const [flag,setFlag]=useState('public')
  const action=useAction()
  const confirm=useConfirm()
  function apply(remove) {
    action.run(async()=>{
      if (!await confirm({title:remove?'Удалить аккаунты?':'Изменить метку?',
        text:remove?`Файлы сессий будут удалены у ${rows.length} аккаунтов.`:`Назначить «${flags[flag]}» аккаунтам (${rows.length})?`,danger:remove})) return
      const errors=[]
      for(const row of rows) {
        try {if(remove)await del(accountPath(row.name));else await post(accountPath(row.name)+'/flag',{flag})}
        catch(error){errors.push(fullName(row)+' · '+error.message)}
      }
      table.resetRowSelection()
      await reload()
      if(errors.length)throw new Error(errors.join(' · '))
    })
  }
  return <div className="grid gap-2">
    <FormActions>
      <DownloadButton filename="veltrix-profiles.csv" rows={[
        ['Имя',...selectedCols.map(([,label])=>label)],
        ...exportRows.map(a=>[fullName(a),...selectedCols.map(([, ,read])=>read(a))]),
      ]}/>
      {!!rows.length&&<>
        <span className="text-sm text-muted-foreground">{rows.length} выбрано</span>
        <Picker items={accountFlags} value={flag} onValueChange={setFlag} aria-label="Метка для выбранных"/>
        <Button variant="outline" disabled={action.busy} onClick={()=>apply(false)}>Назначить</Button>
        <Button variant="outline" color="destructive" disabled={action.busy} onClick={()=>apply(true)}><Trash2/>Удалить</Button>
      </>}
    </FormActions>
    <Problem message={action.error}/>
  </div>
}

export function Profiles({parameters,navigate,settings,query}) {
  const rows=query.data?.items||[]
  const [status,setStatus]=useState(parameters.filter||'all')
  const [tag,setTag]=useState('all')
  const [selected,setSelected]=useState('')
  const [inventory,setInventory]=useState(false)
  const [mobile,setMobile]=useState(()=>localStorage.getItem('veltrix-accounts-column')||'username')
  const wide=useWide()
  const account=rows.find(a=>a.name===selected)
  const frozen=new Set(rows.filter(a=>a.frozen).map(a=>a.id||a.name))
  const active=new Set(rows.filter(a=>a.connected&&!frozen.has(a.id||a.name)).map(a=>a.id||a.name))
  const data=rows.filter(a=>(tag==='all'||(a.flag||'general')===tag)&&
    (status==='all'||status===(frozen.has(a.id||a.name)?'frozen':a.connected?'active':'offline')))
  const schema=useMemo(()=>{
    const optional=columns.filter(([id])=>wide||id===mobile)
    return [...(wide?[dataTableSelectColumn()]:[]),{
      id:'name',accessorFn:a=>fullName(a)+' '+(a.username||'')+' '+a.name,
      header:'Визуальное имя',enableHiding:false,
      cell:({row})=><button type="button" className="flex cursor-pointer items-center gap-2 text-start font-medium hover:text-primary-text" onClick={()=>setSelected(row.original.name)}>
        <AccountAvatar account={row.original}/>
        {fullName(row.original)}
      </button>,
    },...optional.map(([id,header,accessorFn])=>({
      id,header,accessorFn,
      cell:({row,getValue})=>id==='status'?<AccountState account={row.original}/>:id==='flag'?
        <Badge color={row.original.flag==='public'?'info':row.original.flag==='autopilot'?'primary':'neutral'} variant="soft">{getValue()}</Badge>:getValue()??'—',
    }))]
  },[wide,mobile])
  if(inventory)return <SessionsInventory close={()=>setInventory(false)}/>
  if(account)return <Profile account={account} settings={settings} reload={query.reload} back={()=>setSelected('')} deleted={()=>{setSelected('');query.reload()}}/>
  return <>
    <PageHeader title="Профили" description="Аккаунты Telegram, метки и настройки профилей.">
      <Button variant="outline" onClick={()=>setInventory(true)}><Shield/>Безопасность и 2FA</Button>
      <Button onClick={()=>navigate('upload')}><Plus/>Добавить</Button>
    </PageHeader>
    <div className="grid gap-4 sm:grid-cols-3">
      <MetricCard label="Аккаунты" value={query.data?rows.length:null} icon={<Users/>}/>
      <MetricCard label="Активные" value={query.data?active.size:null} icon={<UserRoundCheck/>}/>
      <MetricCard label="Замороженные" value={query.data?frozen.size:null} icon={<Shield/>}/>
    </div>
    <Problem message={query.error}/>
    {(query.data?.load_errors||[]).map(item=><Problem key={item.name} message={item.name+' · '+item.error}/>)}
    <Card title="Список аккаунтов">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Picker items={statuses} value={status} onValueChange={setStatus} aria-label="Статус"/>
        <Picker items={[[ 'all','Все метки'],...accountFlags]} value={tag} onValueChange={setTag} aria-label="Метка"/>
        {!wide&&<Picker items={columns.map(([id,label])=>[id,label])} value={mobile} onValueChange={value=>{setMobile(value);localStorage.setItem('veltrix-accounts-column',value)}} aria-label="Второй столбец"/>}
      </div>
      {!query.data&&!query.error?<Loading/>:<DataTable columns={schema} data={data} getRowId={a=>a.name} autoResetPage={false}
        searchPlaceholder="Поиск по аккаунтам…" emptyMessage="Аккаунты не найдены" toolbar={table=><Bulk table={table} reload={query.reload}/>}/>}
    </Card>
  </>
}

function Profile({account,settings,reload,back,deleted}) {
  const base=accountPath(account.name)
  const action=useAction()
  const confirm=useConfirm()
  const [view,setView]=useState('profile')
  const [feedback,setFeedback]=useState('')
  const details=useData(account.connected?base+'/full':null)
  const [form,setForm]=useState({first_name:account.first_name||'',last_name:account.last_name||'',username:account.username||'',
    bio:account.profile_meta?.bio||'',birthday:account.profile_meta?.birthday||''})
  const [edited,setEdited]=useState({})
  const profile=details.data
  const fields=[['first_name','Имя'],['last_name','Фамилия'],['username','Юзернейм'],['bio','Био'],['birthday','Дата рождения']]
  const update=(key,value)=>{setForm(state=>({...state,[key]:value}));setEdited(state=>({...state,[key]:true}))}
  function apply(path,data,label) {
    action.run(async()=>{
      setFeedback('')
      const result=await post(path,data)
      const errors=result.errors||[]
      await Promise.all([details.reload(),reload()])
      if(errors.length)throw new Error(errors.join(' · '))
      setFeedback(label)
    })
  }
  const tabs=[['profile','Профиль'],['media','Аватарки и музыка'],['copy','Скопировать профиль'],['security','Сессии и 2FA']]
  return <>
    <PageHeader title={fullName(account)} description={`Телефон: ${account.phone||'—'} · ID: ${account.id||'—'}`}>
      <Button variant="outline" onClick={back}><ArrowLeft/>Профили</Button>
    </PageHeader>
    <Card>
      <div className="flex flex-wrap items-center gap-4">
        <AccountAvatar account={account} size="xl"/>
        <div className="grid flex-1 gap-1"><h2 className="font-semibold">{fullName(account)}</h2><p className="text-sm text-muted-foreground">{account.username?'@'+account.username:account.name}</p></div>
        <AccountState account={account}/>
        <Picker items={accountFlags} label="Метка" value={account.flag||'general'} onValueChange={flag=>apply(base+'/flag',{flag},'Метка сохранена')}/>
      </div>
    </Card>
    <FormActions>{tabs.map(([id,label])=><Button key={id} size="sm" variant={view===id?'solid':'outline'} onClick={()=>setView(id)} disabled={!account.connected&&id!=='profile'}>{label}</Button>)}</FormActions>
    <Problem message={action.error||details.error}/>
    {feedback&&<p className="text-sm text-success-text" role="status">{feedback}</p>}
    {view==='profile'&&<>
      {account.connected&&<section className="grid gap-4 xl:grid-cols-2">
        <Card title="Данные профиля">
          <form className="grid gap-3 sm:grid-cols-2" onSubmit={event=>{
            event.preventDefault()
            apply(base+'/profile',Object.fromEntries(fields.filter(([key])=>edited[key]).map(([key])=>[key,form[key]])),'Профиль сохранён')
          }}>
            {fields.map(([key,label])=><Input key={key} label={label} value={!edited[key]&&profile?.[key]!=null?profile[key]:form[key]}
              onChange={event=>update(key,event.target.value)} maxLength={key==='bio'?70:undefined} placeholder={key==='birthday'?'ДД.ММ.ГГГГ':undefined}/>)}
            <div className="sm:col-span-2"><Button type="submit" disabled={action.busy||!Object.keys(edited).length}>Сохранить</Button></div>
          </form>
        </Card>
        <Card title="Приватность">
          <div className="grid gap-4 text-sm">
            <FormActions><Button variant="outline" disabled={action.busy} onClick={()=>apply(base+'/open',{},'Видимость обновлена')}>Открыть био, день рождения и аватарки всем</Button></FormActions>
            <Check checked={!!profile?.last_seen_hidden} disabled={!profile||action.busy} onChange={hidden=>apply(base+'/last-seen',{hidden},'Видимость обновлена')}>Скрыть время захода</Check>
            <div><Button variant="outline" disabled={action.busy} onClick={()=>apply(base+'/spam-status',{},'Статус обновлён')}>Проверить спамбан</Button>
              <span className="ms-2 text-muted-foreground">{account.spam_status?.label||'Не проверен'}</span></div>
          </div>
        </Card>
      </section>}
      <Card title="Удаление аккаунта" description="Удаляет файлы аккаунта из панели."
        action={<Button variant="outline" color="destructive" disabled={action.busy} onClick={()=>action.run(async()=>{
          if(await confirm({title:'Удалить этот аккаунт?',text:fullName(account),danger:true,label:'Удалить'})){await del(base);deleted()}
        })}><Trash2/>Удалить</Button>}/>
    </>}
    {view==='media'&&<ProfileMedia base={base} reload={reload}/>}
    {view==='copy'&&<ProfileCopy base={base} settings={settings} reload={reload}/>}
    {view==='security'&&<Security account={account} reload={reload}/>}
  </>
}

function ProfileMedia({base,reload}) {
  const photos=useData(base+'/profile-photos')
  const action=useAction()
  const confirm=useConfirm()
  const [file,setFile]=useState(null)
  const [music,setMusic]=useState('')
  const [count,setCount]=useState(null)
  return <div className="grid gap-4 xl:grid-cols-2">
    <Card title="Аватарки" description={photos.data?.total!=null?`Всего: ${photos.data.total}`:undefined}>
      <div className="grid gap-3">
        <Input type="file" label="Фото" accept="image/jpeg,image/png,image/webp" onChange={event=>setFile(event.target.files[0])}/>
        <FormActions><Button disabled={!file||action.busy} onClick={()=>action.run(async()=>{await upload(base+'/avatar',file);setFile(null);await photos.reload();reload()})}><Camera/>Установить</Button><Button variant="outline" onClick={photos.reload}><RefreshCw/>Обновить</Button></FormActions>
        <Problem message={photos.error||action.error}/>
        {photos.data&&<div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {photos.data.photos.map(photo=><figure key={photo.id} className="overflow-hidden rounded-lg border border-divider">
            <img src={base+'/profile-photos/'+photo.id+'/image'} alt="Аватарка Telegram" className="aspect-square w-full object-cover" loading="lazy"/>
            <figcaption className="flex items-center justify-between gap-1 p-2 text-xs"><span>{moscow(photo.date,{dateStyle:'short'})}</span>
              <Button size="icon-sm" variant="ghost" color="destructive" aria-label="Удалить фото" disabled={action.busy} onClick={()=>action.run(async()=>{
                if(await confirm({title:'Удалить аватарку из Telegram?',danger:true})){await del(base+'/profile-photos/'+photo.id);await photos.reload();reload()}
              })}><Trash2/></Button>
            </figcaption>
          </figure>)}
        </div>}
      </div>
    </Card>
    <Card title="Музыка" description={count!=null?`Треков в профиле: ${count}`:undefined}>
      <div className="grid gap-3">
        <Input value={music} onChange={event=>setMusic(event.target.value)} label="Ссылка на пост с аудио"/>
        <FormActions>
          <Button disabled={!music.trim()||action.busy} onClick={()=>action.run(async()=>{
            await post(base+'/music',{url:music.trim()});setMusic('');setCount((await api(base+'/music')).count);reload()
          })}>Добавить</Button>
          <Button variant="outline" disabled={action.busy} onClick={()=>action.run(async()=>setCount((await api(base+'/music')).count))}>Обновить число треков</Button>
        </FormActions>
      </div>
    </Card>
  </div>
}

const copyFields={first_name:'Имя',last_name:'Фамилия',username:'Юзернейм',birthday:'День рождения',bio:'Био',avatars:'Аватарки',music:'Музыка'}
const prefsKey='veltrix-profile-copy-preferences'
function ProfileCopy({base,settings,reload}) {
  const path=base+'/profile-copy'
  const job=useData(path,20000)
  const [search,setSearch]=useState(null)
  const searchState=useData(search?path+'/search/'+url(search):path+'/search',search?1200:0)
  const [draft,setDraft]=useState(null)
  const [seenDraft,setSeenDraft]=useState(null)
  const [mode,setMode]=useState('group')
  const [source,setSource]=useState('')
  const [prefs,setPrefs]=useState(()=>{
    let previous={}
    try {previous=JSON.parse(localStorage.getItem(prefsKey)||'{}')} catch {}
    return {fields:[],avatar_min:3,avatar_max:5,delay_from_days:1,delay_to_days:2,...previous}
  })
  const action=useAction()
  function change(key,value) {
    setPrefs(previous=>{
      const next={...previous,[key]:value}
      localStorage.setItem(prefsKey,JSON.stringify(next))
      return next
    })
  }
  const state=searchState.data
  useEffect(()=>{
    if(state?.state==='ready'&&state.draft?.token!==seenDraft) {
      setSeenDraft(state.draft?.token)
      setDraft(state.draft)
      setSearch(null)
    } else if(state?.token&&!search&&['queued','searching','waiting'].includes(state.state)) {
      setSearch(state.token)
    } else if(state?.state==='error') {
      action.setError(state.error||'Поиск не выполнен')
      setSearch(null)
    }
  },[state,search,seenDraft])
  const group=settings?.profile_group||'https://t.me/minecraft_beseda764'
  const profileDraft=draft?path+'/'+draft.token:''
  const active=job.data&&['waiting','running'].includes(job.data.state)
  return <Card title="Скопировать профиль" description="В случайном поиске галочки задают обязательные свойства.">
    <form className="grid gap-4" onSubmit={event=>{
      event.preventDefault()
      action.run(async()=>{
        const result=await post(path+'/search',{
          group:source.trim()||(mode==='group'?group:''),mode,fields:mode==='group'?prefs.fields:[],
          avatar_min:mode==='group'?Number(prefs.avatar_min):1,avatar_max:mode==='group'?Number(prefs.avatar_max):1,
          workers:settings?.search_workers||5,delay_from_days:prefs.delay_from_days,delay_to_days:prefs.delay_to_days,
        })
        setSearch(result.token)
      })
    }}>
      <Picker label="Источник" items={[[ 'group','Случайный участник группы'],['profile','Профиль по ссылке']]} value={mode} onValueChange={setMode}/>
      <Input label={mode==='group'?'Группа Telegram':'Ссылка на профиль'} placeholder={mode==='group'?group:'https://t.me/username'} required={mode==='profile'} value={source} onChange={event=>setSource(event.target.value)}/>
      {mode==='group'&&<div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        {Object.entries(copyFields).slice(2).map(([field,label])=><Check key={field} checked={prefs.fields.includes(field)}
          onChange={checked=>change('fields',checked?[...prefs.fields,field]:prefs.fields.filter(item=>item!==field))}>{label}</Check>)}
      </div>}
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {[['avatar_min','Аватарок от',1],['avatar_max','Аватарок до',1],['delay_from_days','Задержка от, дней',0],['delay_to_days','Задержка до, дней',0]]
          .filter(([key])=>mode==='group'||!key.startsWith('avatar'))
          .map(([key,label,min])=><Input key={key} label={label} type="number" min={min} step={min?'1':'any'} value={prefs[key]} onChange={event=>change(key,event.target.value)} required/>)}
      </div>
      <Problem message={action.error||searchState.error||job.error}/>
      {search&&<p className="text-sm text-muted-foreground">{state?.state==='waiting'?`Пауза Telegram: ${state.retry_in} сек.`:'Подбираю профиль…'} {state?.checked?`Проверено: ${state.checked}`:''}</p>}
      {job.data?.state&&<p className="text-sm text-muted-foreground">{active?'Перенос активен':'Перенос завершён'} · аватарки {job.data.uploaded}/{job.data.total}{job.data.due?' · следующий шаг '+moscow(job.data.due):''}</p>}
      {(job.data?.errors||[]).map(text=><Problem key={text} message={text}/>)}
      <FormActions>
        <Button type="submit" disabled={action.busy||!!search||active}>Предпросмотр</Button>
        {search&&<Button type="button" variant="outline" onClick={()=>action.run(async()=>{await del(path+'/search/'+url(search));setSearch(null)})}>Остановить поиск</Button>}
      </FormActions>
    </form>
    <Dialog open={!!draft} onOpenChange={open=>{if(!open&&draft&&!action.busy){del(profileDraft).catch(()=>{});setDraft(null)}}}
      title="Применить новый профиль?" description={draft?`Источник Telegram ID ${draft.source_id}`:undefined} size="xl"
      footer={<><Button variant="outline" disabled={action.busy} onClick={()=>{del(profileDraft).catch(()=>{});setDraft(null)}}>Нет</Button>
        <Button disabled={action.busy} onClick={()=>action.run(async()=>{await post(profileDraft);setDraft(null);job.reload();reload()})}>Да, применить</Button></>}>
      {draft&&<div className="grid gap-3">
        {Object.entries(draft.values||{}).map(([field,value])=><div key={field} className="flex justify-between gap-2 border-b border-divider pb-2 text-sm"><span>{copyFields[field]||field}</span><span>{String(value)}</span></div>)}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">{(draft.delays||[]).map((delay,i)=><figure key={i}><img className="aspect-square w-full rounded-lg object-cover" src={profileDraft+'/photos/'+i} alt={'Аватарка '+(i+1)}/><figcaption className="text-xs text-muted-foreground">Через {(delay/86400).toFixed(2)} дн.</figcaption></figure>)}</div>
        <Problem message={action.error}/>
      </div>}
    </Dialog>
  </Card>
}

function Security({account,reload}) {
  const inventory=useData('/api/2fa?name='+url(account.name))
  const action=useAction()
  const confirm=useConfirm()
  const [sessions,setSessions]=useState([])
  const item=inventory.data?.items?.find(item=>item.name===account.name)
  return <Card title="Сессии и 2FA" description={`Последняя проверка: ${moscow(account.security?.sessions_checked_at)}`}>
    <div className="grid gap-4">
      <Input label="Пароль 2FA панели" type="password" value={item?.password||''} readOnly placeholder="Пароль не сохранён"/>
      <FormActions>
        <Button variant="outline" disabled={action.busy} onClick={()=>action.run(async()=>{
          const result=await post('/api/2fa/check',{name:account.name})
          setSessions(result.authorizations||[]);reload()
        })}><RefreshCw/>Обновить сессии</Button>
        <Button variant="outline" color="destructive" disabled={action.busy} onClick={()=>action.run(async()=>{
          if(await confirm({title:'Отключить все другие сессии?',text:fullName(account),danger:true})){
            const result=await post('/api/2fa/reset-others',{name:account.name})
            setSessions(result.authorizations||[]);reload()
          }
        })}>Отключить все кроме своей</Button>
      </FormActions>
      <Problem message={action.error||inventory.error}/>
      {sessions.map((session,index)=><div key={index} className="rounded-lg border border-divider p-3 text-sm">
        {session.current?'● Текущая':'○ Другая'} · {session.app||session.device||'Устройство'} · {moscow(session.created_at)}
      </div>)}
    </div>
  </Card>
}

function SessionsInventory({close}) {
  const data=useData('/api/2fa',12000)
  const [pending,setPending]=useState(null)
  const [password,setPassword]=useState('')
  const action=useAction()
  const confirm=useConfirm()
  function reissue(name) {
    action.run(async()=>{
      if(!await confirm({title:name?'Создать независимую сессию?':'Перевыпустить внешние сессии?',text:name||'Панель применит процедуру ко всем подходящим аккаунтам.'}))return
      await post(name?'/api/2fa/reissue-one':'/api/2fa/reissue',name?{name}:{})
      await data.reload()
    })
  }
  const columns=[
    {accessorKey:'name',header:'Аккаунт'},
    {accessorKey:'phone',header:'Телефон'},
    {id:'owned',header:'Происхождение',accessorFn:row=>row.owned?'Своя':'Внешняя'},
    {id:'password',header:'Пароль 2FA',cell:({row})=>row.original.password?<Input type="password" value={row.original.password} readOnly aria-label="Пароль 2FA"/>:'—'},
    {id:'received',header:'Получена (МСК)',accessorFn:row=>row.received_at,cell:({getValue})=>moscow(getValue())},
    {id:'created',header:'Авторизация (МСК)',accessorFn:row=>row.session_date,cell:({getValue})=>moscow(getValue())},
    {accessorKey:'status',header:'Статус'},
    {id:'command',header:'Действие',cell:({row})=>row.original.needs_password?
      <Button size="sm" variant="outline" disabled={action.busy} onClick={()=>setPending(row.original.name)}>Ввести пароль 2FA</Button>:
      <Button size="sm" variant="outline" disabled={row.original.owned||!row.original.working||action.busy||data.data?.job?.running} onClick={()=>reissue(row.original.name)}>Перевыпустить</Button>},
  ]
  return <>
    <PageHeader title="Безопасность и 2FA"><Button variant="outline" onClick={close}><ArrowLeft/>Профили</Button><Button disabled={action.busy} onClick={()=>reissue()}>Перевыпустить внешние</Button></PageHeader>
    <Problem message={data.error||action.error}/>
    <Card title="Сессии" description={data.data?.job?.running?'Перевыпуск выполняется':undefined}>{data.data?<DataTable columns={columns} data={data.data.items||[]} getRowId={row=>row.name}/>:!data.error&&<Loading/>}</Card>
    <Dialog open={!!pending} onOpenChange={open=>{if(!open&&!action.busy){setPending(null);setPassword('')}}}
      title="Пароль для перевыпуска" description={pending||undefined}
      form={{onSubmit:event=>{
        event.preventDefault()
        action.run(async()=>{
          await post('/api/2fa/password',{name:pending,password});setPending(null);setPassword('');await data.reload()
        })
      }}} footer={<Button type="submit" disabled={!password||action.busy}>Продолжить</Button>}>
      <div className="grid gap-3"><Input type="password" label="Текущий пароль 2FA" value={password} onChange={event=>setPassword(event.target.value)} required/><Problem message={action.error}/></div>
    </Dialog>
  </>
}
