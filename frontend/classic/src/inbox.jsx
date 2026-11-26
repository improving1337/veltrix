import {useEffect,useRef,useState} from 'react'
import {ArrowLeft, CheckCheck, Download, Ellipsis, ImagePlus, MessageSquare, Search, Send, SquarePen, X} from 'lucide-react'
import {Avatar} from '@dashral/ui/components/avatar'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {accountPath,connected,moscow,post,upload,useAction,useData} from './api'
import {AccountPicker,Empty,FormActions,Loading,Problem,useConfirm} from './ui'
import {PageHeader} from './page-header'

const key=chat=>chat.account+':'+chat.chat_id
const time=value=>moscow(value,{hour:'2-digit',minute:'2-digit'})
const chatPath=chat=>accountPath(chat.account)+'/chats/'+chat.chat_id
const safeLink=link=>typeof link==='string'&&/^(https?:|tg:)/i.test(link)

export function Inbox({inbox,accounts}) {
  const [query,setQuery]=useState('')
  const [active,setActive]=useState(null)
  const [dialog,setDialog]=useState('')
  const [account,setAccount]=useState('')
  const [recipient,setRecipient]=useState('')
  const [count,setCount]=useState(20)
  const [notice,setNotice]=useState('')
  const action=useAction()
  const confirm=useConfirm()
  const items=inbox.data?.items||[]
  const eligible=connected(accounts)
  const visible=items.filter(chat=>[chat.title,chat.last_text,chat.account].some(value=>String(value||'').toLocaleLowerCase('ru').includes(query.trim().toLocaleLowerCase('ru'))))
  function open(chat) {
    setActive(chat)
    if(chat.unread)post(chatPath(chat)+'/read').then(inbox.reload).catch(error=>action.setError(error.message))
  }
  function submitDialog(event) {
    event.preventDefault()
    action.run(async()=>{
      if(!account)throw new Error('Выберите аккаунт')
      if(dialog==='load') {
        const result=await post('/api/inbox/load-dialogs',{accounts:[account],count:Number(count)})
        setNotice(`Загружено: ${result.added}`)
        if(result.errors?.length)action.setError(result.errors.join(' · '))
        await inbox.reload()
      }else{
        const chat=await post(accountPath(account)+'/resolve',{query:recipient.trim()})
        open({...chat,account});setRecipient('')
      }
      setDialog('')
    })
  }
  function readAll() {
    action.run(async()=>{
      if(!await confirm({title:'Прочитать все входящие?',text:'Telegram отметит сообщения в доступных диалогах.'}))return
      const result=await post('/api/inbox/read-all')
      setNotice(`Обновлено диалогов: ${result.read}`)
      if(result.errors)action.setError(`Не удалось обновить диалоги: ${result.errors}`)
      await inbox.reload()
    })
  }
  return <>
    <PageHeader title="Inbox"/>
    <Problem message={inbox.error||action.error}/>
    {notice&&<p className="text-sm text-muted-foreground" role="status">{notice}</p>}
    <div className="grid min-h-[34rem] overflow-hidden rounded-xl border border-divider bg-card md:h-[calc(100svh-10rem)] md:grid-cols-[minmax(250px,30%)_minmax(0,1fr)]">
      <aside className={`min-w-0 grid-rows-[auto_auto_minmax(0,1fr)] border-divider md:grid md:border-e ${active?'hidden':'grid'}`}>
        <div className="flex items-start justify-between gap-2 p-4 pb-3">
          <div><h2 className="text-lg font-semibold tracking-tight">Чаты</h2><p className="text-sm text-muted-foreground">{inbox.data?.unread||0} непрочитанных</p></div>
          <div className="flex flex-wrap gap-1">
            <Button size="icon-sm" variant="outline" onClick={()=>setDialog('load')} aria-label="Загрузить диалоги" title="Загрузить диалоги"><Download/></Button>
            <Button size="icon-sm" variant="outline" onClick={()=>setDialog('new')} aria-label="Написать" title="Написать"><SquarePen/></Button>
          </div>
        </div>
        <div className="grid gap-2 px-4 pb-3">
          <Input prefix={<Search/>} value={query} onChange={event=>setQuery(event.target.value)} type="search" placeholder="Поиск диалогов" aria-label="Поиск диалогов"/>
          <Button variant="ghost" size="sm" disabled={!inbox.data?.unread||action.busy} onClick={readAll} className="justify-start"><CheckCheck/>Прочитать всё</Button>
        </div>
        <div className="thin-scrollbar min-h-0 space-y-1 overflow-y-auto px-3 pb-3">
          {!inbox.data&&!inbox.error?<Loading/>:!visible.length?<Empty>{query?'Диалоги не найдены':'Входящих диалогов пока нет.'}</Empty>:
            visible.map(chat=><button key={key(chat)} type="button" onClick={()=>open(chat)}
              className={`flex w-full cursor-pointer items-center gap-3 rounded-lg p-2.5 text-start transition-colors hover:bg-muted/70 ${active&&key(active)===key(chat)?'bg-muted':''}`}>
              <Avatar src={chatPath(chat)+'/avatar'} name={chat.title} size="lg"/>
              <span className="grid min-w-0 flex-1 gap-0.5">
                <span className="flex items-baseline justify-between gap-2"><span className="truncate text-sm font-medium">{chat.title}</span><small className="text-muted-foreground">{time(chat.date)}</small></span>
                <span className="flex items-center justify-between gap-2"><span className="truncate text-xs text-muted-foreground">{chat.last_text}</span>{chat.unread>0&&<Badge size="sm">{chat.unread}</Badge>}</span>
              </span>
            </button>)}
        </div>
      </aside>
      {active?<Chat key={key(active)} chat={active} reloadInbox={inbox.reload} close={()=>setActive(null)}/>:
        <section className="hidden min-h-0 place-items-center md:grid"><div className="grid place-items-center gap-3 text-sm text-muted-foreground"><MessageSquare className="size-7"/>Выберите диалог</div></section>}
    </div>
    <Dialog open={!!dialog} onOpenChange={open=>{if(!open)setDialog('')}} title={dialog==='load'?'Загрузить недавние диалоги':'Новое сообщение'}
      form={{onSubmit:submitDialog}} footer={<Button type="submit" disabled={action.busy||!eligible.length}>{dialog==='load'?'Загрузить':'Открыть чат'}</Button>}>
      <div className="grid gap-3">
        <AccountPicker items={eligible} value={account} onChange={setAccount}/>
        {dialog==='load'?<Input label="Количество диалогов" type="number" min={1} max={100} value={count} onChange={event=>setCount(event.target.value)}/>:
          <Input label="Кому" value={recipient} onChange={event=>setRecipient(event.target.value)} placeholder="@username, ссылка, телефон или ID" required/>}
        <Problem message={action.error}/>
      </div>
    </Dialog>
  </>
}

function mergeMessages(items,messages) {
  const byId=new Map(items.map(item=>[item.id,item]))
  for(const item of messages)byId.set(item.id,item)
  return [...byId.values()].sort((a,b)=>a.id-b.id).slice(-300)
}
function Chat({chat,reloadInbox,close}) {
  const path=chatPath(chat)
  const history=useData(path+'/messages')
  const [messages,setMessages]=useState([])
  const [blocked,setBlocked]=useState(null)
  const [text,setText]=useState('')
  const [file,setFile]=useState(null)
  const [preview,setPreview]=useState('')
  const [menu,setMenu]=useState(false)
  const [note,setNote]=useState('')
  const composer=useRef(null)
  const scroll=useRef(null)
  const follow=useRef(true)
  const action=useAction()
  const confirm=useConfirm()
  useEffect(()=>{
    if(history.data) {
      setMessages(previous=>mergeMessages(history.data.messages,previous))
      setBlocked(history.data.blocked)
    }
  },[history.data])
  useEffect(()=>{
    if(!file){setPreview('');return}
    const object=URL.createObjectURL(file)
    setPreview(object)
    return ()=>URL.revokeObjectURL(object)
  },[file])
  useEffect(()=>{
    function listener(event) {
      const data=event.detail
      if(data.account!==chat.account||data.chat_id!==chat.chat_id||!data.message)return
      if(data.type==='message'||data.type==='edit')setMessages(previous=>mergeMessages(previous,[data.message]))
      if(data.type==='message'&&!data.out&&document.visibilityState==='visible')post(path+'/read').catch(()=>{})
    }
    window.addEventListener('veltrix:event',listener)
    return ()=>window.removeEventListener('veltrix:event',listener)
  },[chat.account,chat.chat_id,path])
  useEffect(()=>{
    if(scroll.current&&follow.current)scroll.current.scrollTop=scroll.current.scrollHeight
  },[messages])
  function send(event) {
    event.preventDefault()
    if(!text.trim()&&!file)return
    action.run(async()=>{
      const body=file?await upload(path+'/send',file,{text:text.trim()},'photo'):await post(path+'/send',{text:text.trim()})
      follow.current=true;setMessages(previous=>mergeMessages(previous,[body.message]));setText('');setFile(null);reloadInbox()
    })
  }
  function press(message,button) {
    if(button.kind==='url'&&safeLink(button.url)){window.open(button.url,'_blank','noopener');return}
    if(button.kind==='copy'){action.run(async()=>{await navigator.clipboard.writeText(button.copy);setNote('Скопировано')});return}
    if(!['web','cb'].includes(button.kind))return
    const popup=button.kind==='web'?window.open('about:blank','_blank'):null
    if(popup)popup.opener=null
    action.run(async()=>{
      try{
        const result=await post(path+(button.kind==='web'?'/webview':'/click'),{message_id:message.id,row:button.row,col:button.col})
        if(result.note)setNote(result.note)
        if(safeLink(result.url)){if(popup)popup.location.href=result.url;else window.open(result.url,'_blank','noopener')}
        else popup?.close()
      }catch(error){popup?.close();throw error}
    })
  }
  const headers=[]
  let lastDay
  for(const message of messages){
    const day=moscow(message.date,{dateStyle:'medium'})
    headers.push(day!==lastDay?day:null);lastDay=day
  }
  return <section className="grid min-h-0 min-w-0 grid-rows-[auto_minmax(0,1fr)_auto]">
    <header className="flex min-w-0 items-center gap-3 border-b border-divider px-4 py-3">
      <Button size="icon-sm" variant="ghost" className="md:hidden" onClick={close} aria-label="Диалоги"><ArrowLeft/></Button>
      <Avatar src={path+'/avatar'} name={chat.title} size="lg"/>
      <div className="min-w-0 flex-1"><h2 className="truncate font-semibold">{chat.title}</h2><p className="truncate text-xs text-muted-foreground">{chat.account}</p></div>
      <Button variant="ghost" size="icon-sm" onClick={()=>setMenu(value=>!value)} aria-expanded={menu} aria-label="Действия чата"><Ellipsis/></Button>
      {menu&&chat.kind==='user'&&<Button variant="outline" color={blocked?'neutral':'destructive'} size="sm" onClick={()=>action.run(async()=>{
        if(!blocked&&!await confirm({title:'Заблокировать собеседника?',text:chat.title,danger:true}))return
        setBlocked((await post(path+'/block',{block:!blocked})).blocked);setMenu(false)
      })}>{blocked?'Разблокировать':'Заблокировать'}</Button>}
    </header>
    <div className="thin-scrollbar min-h-[20rem] space-y-4 overflow-y-auto px-4 py-5 md:min-h-0" ref={scroll} onScroll={()=>{
      const node=scroll.current;follow.current=node.scrollHeight-node.scrollTop-node.clientHeight<80
    }}>
      {!history.data&&!history.error?<Loading/>:!messages.length&&<Empty>Сообщений пока нет.</Empty>}
      {messages.map((message,index)=><div key={message.id} className="grid gap-2">
        {headers[index]&&<p className="mx-auto rounded-full border border-divider bg-background px-3 py-1 text-xs text-muted-foreground">{headers[index]}</p>}
        <div className={`flex items-end gap-2 ${message.out?'justify-end':'justify-start'}`}>
          {!message.out&&<Avatar name={message.sender||chat.title} src={path+'/avatar'} size="sm"/>}
          <div className="grid max-w-[83%] min-w-0 gap-1">
            <div className={`rounded-xl px-3 py-2 text-sm ${message.out?'bg-primary text-primary-foreground':'bg-muted text-foreground'}`}>
              {!message.out&&chat.kind!=='user'&&message.sender&&<p className="text-xs font-semibold">{message.sender}</p>}
              {message.has_media&&(message.media_kind==='photo'?<a href={path+'/media/'+message.id} target="_blank" rel="noreferrer"><img className="max-h-64 max-w-full rounded-lg object-contain" src={path+'/media/'+message.id} alt="Фото" loading="lazy"/></a>:
                <a className="flex items-center gap-2 underline" href={path+'/media/'+message.id+'?download=1'} download><Download className="size-4"/>{message.file_name||message.media||'Файл'}</a>)}
              {message.text?<p className="whitespace-pre-wrap break-words">{message.text}</p>:!message.has_media&&<p>{message.media||'Служебное сообщение'}</p>}
              <time className="block text-end text-[11px] opacity-75">{time(message.date)}</time>
            </div>
            {(message.buttons||[]).map((row,i)=><div key={i} className="flex flex-wrap gap-1">{row.map((button,j)=>
              <Button key={j} variant="outline" size="sm" disabled={action.busy||button.kind==='other'} onClick={()=>press(message,button)}>{button.text}</Button>)}</div>)}
          </div>
        </div>
      </div>)}
    </div>
    <form className="grid gap-2 border-t border-divider p-3" onSubmit={send}>
      <Problem message={history.error||action.error}/>
      {note&&<p className="text-xs text-muted-foreground" role="status">{note}</p>}
      {file&&<div className="flex items-center gap-2 text-sm">{preview&&<img src={preview} alt="Прикреплённое фото" className="size-10 rounded-md object-cover"/>}<span className="truncate">{file.name}</span>
        <Button type="button" variant="ghost" size="icon-sm" onClick={()=>setFile(null)} aria-label="Убрать фото"><X/></Button></div>}
      <div className="flex min-w-0 items-end gap-2 rounded-lg border border-input bg-background p-2">
        <input ref={composer} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={event=>{setFile(event.target.files[0]||null);event.target.value=''}}/>
        <Button type="button" size="icon-sm" variant="ghost" aria-label="Прикрепить фото" onClick={()=>composer.current.click()}><ImagePlus/></Button>
        <textarea className="max-h-36 min-w-0 flex-1 resize-y bg-transparent px-2 py-1 text-sm outline-none" rows={1} value={text}
          onChange={event=>setText(event.target.value)} aria-label="Сообщение" placeholder="Написать сообщение…"
          onKeyDown={event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(!action.busy)event.currentTarget.form.requestSubmit()}}}
          onPaste={event=>{const image=[...event.clipboardData.files].find(item=>item.type.startsWith('image/'));if(image){event.preventDefault();setFile(image)}}}/>
        <Button type="submit" size="icon-sm" disabled={action.busy||(!text.trim()&&!file)} aria-label="Отправить"><Send/></Button>
      </div>
    </form>
  </section>
}
