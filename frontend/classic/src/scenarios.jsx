import {useEffect,useRef,useState} from 'react'
import {BookOpen, ClipboardList, ImagePlus, Play, Terminal, X} from 'lucide-react'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import SortableTable from './tables'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {moscow,post,upload,useAction,useData} from './api'
import {Check,Empty,FormActions,Picker,Problem,TextArea,useConfirm} from './ui'
import {PageHeader} from './page-header'

const example='for acc in all:\n    views(acc, param("post"), unique=True)\n    wait(10, 15)'
const labels={views:'Просмотр',sub:'Подписка',unsub:'Отписка',react:'Реакция',poll:'Голосование',repost:'Репост в Избранное',start:'Запуск бота',button:'Кнопка под постом',online:'Выход в онлайн',send:'Сообщение',send_photo:'Отправка фото',bot_button:'Кнопка бота',wait:'Пауза'}
const status={running:'Выполняется',paused:'Пауза',uncertain:'Проверьте текущий шаг',stopped:'Остановлен',finished:'Завершён',ok:'Успех',skip:'Пропуск',err:'Ошибка',wait:'Ожидание'}
const simulationStatus={recorded:'Уже выполнено по истории',membership:'Подписка проверится при запуске',depends:'Пропуск, если предыдущий шаг выполнится'}
const docs=[
  ['Аккаунты','all - [123, 456]','Подключённые аккаунты с метками «Общий» и «Публичный». Список справа исключает указанные Telegram ID.'],
  ['Аккаунты','accounts(flag=autopilot)','Выбор отдельной метки. Поддерживаются autopilot, general и public.'],
  ['Аккаунты','group("Основные") · random_accounts(all, 10)','Сохранённая группа и случайная выборка разных аккаунтов. Предпросмотр фиксирует выборку.'],
  ['Аккаунты','for acc in all: · account=123','Цикл идёт по аккаунтам, строки внутри имеют отступ из четырёх пробелов. Для одного Telegram ID можно передать account=123.'],
  ['Telegram','views(acc, post, unique=True) · repost(acc, post)','Просмотр и репост в Избранное. unique=True учитывает успешные действия из истории, а для подписок проверяет текущее участие.'],
  ['Telegram','sub(acc, channel) · unsub(acc, channel) · online(acc, 60)','Подписка, отписка и выход в онлайн на 60 секунд.'],
  ['Telegram','react(acc, post, "❤️") · poll(acc, post, 1)','Реакция и голосование. Номер варианта начинается с 1.'],
  ['Боты','start(acc, bot) · button(acc, post) · bot_button(acc, bot, 1, text="❤️")','Запуск бота (с поддержкой параметра start) и кнопки под постом или в свежем сообщении бота.'],
  ['Сообщения','send(acc, "@username", "Текст") · send_photo(acc, "@bot", photo())','Сообщение и отправка прикреплённого фото. Вставить фото можно через Ctrl+V. me открывает Избранное.'],
  ['Параметры','wait(10) · wait(10, 15) · param("post")','Обычная или случайная пауза в секундах, подстановка значения из JSON-параметров.'],
]

export function Scenarios({parameters}) {
  const live=useData('/api/scenarios',2000)
  const [code,setCode]=useState(()=>localStorage.getItem('veltrix.script')??example)
  const [params,setParams]=useState('{"post":"https://t.me/test/1"}')
  const [start,setStart]=useState('')
  const [from,setFrom]=useState('')
  const [to,setTo]=useState('')
  const [stopOnError,setStopOnError]=useState(true)
  const [image,setImage]=useState('')
  const [file,setFile]=useState('')
  const [wiki,setWiki]=useState(!!parameters.wiki)
  const [plan,setPlan]=useState(null)
  const [folder,setFolder]=useState('')
  const [groupName,setGroupName]=useState('')
  const [ids,setIds]=useState('')
  const [template,setTemplate]=useState('')
  const [templateName,setTemplateName]=useState('')
  const fileInput=useRef(null)
  const action=useAction()
  const confirm=useConfirm()
  const run=live.data?.run
  const isRunning=['running','paused','uncertain'].includes(run?.status)
  function set(update,value) {update(value);setPlan(null)}
  function payload() {
    let json
    try{json=JSON.parse(params||'{}')}catch{throw new Error('Проверьте JSON-параметры')}
    return {code,params:json,image:image||null,stop_on_error:stopOnError,quiet_hours:from||to?{from,to}:null,start_at:start?Date.parse(start+'+03:00')/1000:null}
  }
  function attach(value) {
    if(!value)return
    action.run(async()=>{
      setImage((await upload('/api/scenarios/photo',value,{},'photo')).image)
      setFile(value.name)
      setPlan(null)
    })
  }
  function execute(command,args) {
    action.run(async()=>{
      if(command==='resume'&&run?.status==='uncertain'&&!await confirm({title:'Повторить неподтверждённый шаг?'}))return
      if(command==='stop'&&!await confirm({title:'Остановить сценарий?'}))return
      await post('/api/scenarios/'+command,args)
      await live.reload()
    })
  }
  function preview() {
    action.run(async()=>{setPlan(null);setPlan(await post('/api/scenarios/preview',payload()))})
  }
  function launch() {
    action.run(async()=>{
      if(!plan||!await confirm({title:'Запустить утверждённый сценарий?',text:`Шагов: ${plan.steps.length}`,label:'Запустить'}))return
      await post('/api/scenarios/start',{...payload(),token:plan.token,seed:plan.seed})
      setPlan(null);await live.reload()
    })
  }
  const groups=live.data?.groups||{},templates=live.data?.templates||{}
  return <>
    <PageHeader title="Сценарии" description="Редактор, предпросмотр и журнал выполнения.">
      <Button onClick={launch} disabled={!plan||isRunning||action.busy}><Play/>Запуск</Button>
      <Button variant="outline" onClick={preview} disabled={!code.trim()||action.busy}><ClipboardList/>Предпросмотр</Button>
      <Button variant="outline" onClick={()=>setWiki(true)}><BookOpen/>Вики</Button>
    </PageHeader>
    <Problem message={action.error||live.error}/>
    <Card title="Сценарий" description="Ctrl+Enter строит предпросмотр.">
      <div className="grid gap-4">
        <Editor value={code} change={value=>{localStorage.setItem('veltrix.script',value);set(setCode,value)}} attach={attach} preview={preview}/>
        <FormActions>
          <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" hidden onChange={event=>{attach(event.target.files[0]);event.target.value=''}}/>
          <Button variant="outline" onClick={()=>fileInput.current.click()} disabled={action.busy}><ImagePlus/>Прикрепить фото</Button>
          {file&&<Badge variant="soft" color="neutral">{file}<Button size="icon-sm" variant="ghost" aria-label="Убрать фото" onClick={()=>{setFile('');set(setImage,'')}}><X/></Button></Badge>}
        </FormActions>
        <details className="rounded-lg border border-divider p-3">
          <summary className="cursor-pointer text-sm font-medium">Параметры и время запуска</summary>
          <div className="mt-3 grid gap-3">
            <TextArea label="Параметры JSON" rows={3} value={params} onChange={event=>set(setParams,event.target.value)}/>
            <div className="grid gap-2 sm:grid-cols-3">
              <Input label="Запуск (МСК)" type="datetime-local" value={start} onChange={event=>set(setStart,event.target.value)}/>
              <Input label="Пропуск с (МСК)" type="time" value={from} onChange={event=>set(setFrom,event.target.value)}/>
              <Input label="Пропуск до (МСК)" type="time" value={to} onChange={event=>set(setTo,event.target.value)}/>
            </div>
            <Check checked={stopOnError} onChange={value=>set(setStopOnError,value)}>Пауза при ошибке</Check>
          </div>
        </details>
        <details className="rounded-lg border border-divider p-3">
          <summary className="cursor-pointer text-sm font-medium">Группы и шаблоны</summary>
          <div className="mt-3 grid gap-4 lg:grid-cols-2">
            <div className="grid gap-2">
              <Picker label="Группа" placeholder="Выберите группу" items={Object.keys(groups).map(id=>[id,id])} value={folder} onValueChange={name=>{setFolder(name);setGroupName(name);setIds(groups[name].join(' '))}}/>
              <Input label="Имя группы" value={groupName} onChange={event=>setGroupName(event.target.value)}/>
              <Input label="Telegram ID через пробел" value={ids} onChange={event=>setIds(event.target.value)}/>
              <FormActions>
                <Button variant="outline" disabled={action.busy||!groupName.trim()} onClick={()=>execute('group',{name:groupName.trim(),ids:ids.trim().split(/[\s,]+/).filter(Boolean).map(Number)})}>Сохранить группу</Button>
                <Button variant="ghost" color="destructive" disabled={action.busy||!groups[groupName]} onClick={()=>execute('group',{name:groupName,delete:true})}>Удалить</Button>
              </FormActions>
            </div>
            <div className="grid gap-2 self-start">
              <Picker label="Шаблон кода" placeholder="Выберите шаблон" items={Object.keys(templates).map(id=>[id,id])} value={template} onValueChange={name=>{setTemplate(name);setTemplateName(name);set(setCode,templates[name])}}/>
              <Input label="Имя шаблона" value={templateName} onChange={event=>setTemplateName(event.target.value)}/>
              <FormActions>
                <Button variant="outline" disabled={action.busy||!templateName.trim()} onClick={()=>execute('template',{name:templateName.trim(),code})}>Сохранить шаблон</Button>
                <Button variant="ghost" color="destructive" disabled={action.busy||!templates[templateName]} onClick={()=>execute('template',{name:templateName,delete:true})}>Удалить</Button>
              </FormActions>
            </div>
          </div>
        </details>
      </div>
    </Card>
    {plan&&<PlanPreview plan={plan}/>}
    <RunLog run={run} disabled={action.busy} execute={execute}/>
    <Wiki open={wiki} setOpen={setWiki}/>
  </>
}

function Editor({value,change,attach,preview}) {
  const [position,setPosition]=useState({left:0,top:0})
  const token=/#.*$|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\b(?:for|in|True|False|None)\b|\b\d+(?:\.\d+)?\b|\b[A-Za-z_]\w*(?=\s*\()/gm
  const colors={keyword:'text-highlight-text',string:'text-success-text',number:'text-warning-text',comment:'text-muted-foreground',function:'text-info-text'}
  const nodes=[]
  let offset=0
  for(const match of value.matchAll(token)){
    const text=match[0],first=text[0]
    const kind=first==='#'?'comment':first==="'"||first==='"'?'string':/^\d/.test(text)?'number':/^(for|in|True|False|None)$/.test(text)?'keyword':'function'
    nodes.push(value.slice(offset,match.index),<span key={match.index} className={colors[kind]}>{text}</span>)
    offset=match.index+text.length
  }
  nodes.push(value.slice(offset)+'\n')
  const lines=value.split('\n')
  return <div className="grid min-w-0 grid-cols-[3rem_minmax(0,1fr)] overflow-hidden rounded-lg border border-divider bg-background">
    <div className="relative overflow-hidden border-e border-divider bg-muted/35" aria-hidden>
      <pre className="absolute inset-x-0 px-2 py-3 text-end font-mono text-[13px] leading-[1.5rem] text-muted-foreground" style={{translate:'0 -'+position.top+'px'}}>{lines.map((_,index)=>index+1).join('\n')}</pre>
    </div>
    <div className="relative min-w-0 overflow-hidden">
      <pre className="absolute inset-0 pointer-events-none overflow-visible whitespace-pre px-3 py-3 font-mono text-[13px] leading-[1.5rem] text-foreground [tab-size:4]" style={{translate:-position.left+'px '+-position.top+'px'}} aria-hidden>{nodes}</pre>
      <textarea value={value} onChange={event=>change(event.target.value)} onScroll={event=>setPosition({left:event.currentTarget.scrollLeft,top:event.currentTarget.scrollTop})}
        className="relative block h-64 w-full resize-y whitespace-pre bg-transparent px-3 py-3 font-mono text-[13px] leading-[1.5rem] text-transparent caret-foreground outline-none selection:bg-primary/30 selection:text-transparent [tab-size:4]" aria-label="Код сценария" spellCheck={false} maxLength={20000}
        onKeyDown={event=>{
          if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();preview()}
          if(event.key==='Tab'&&!event.shiftKey){
            event.preventDefault()
            const node=event.currentTarget,at=node.selectionStart
            change(value.slice(0,at)+'    '+value.slice(node.selectionEnd))
            requestAnimationFrame(()=>node.setSelectionRange(at+4,at+4))
          }
        }}
        onPaste={event=>{const photo=[...event.clipboardData.files].find(item=>item.type.startsWith('image/'));if(photo){event.preventDefault();attach(photo)}}}/>
    </div>
  </div>
}

function PlanPreview({plan}) {
  const steps=plan.steps.map((step,index)=>({step,index,simulation:plan.simulation.steps[index]}))
  const columns=[
    {accessorKey:'index',header:'#',cell:({getValue})=>getValue()+1},
    {id:'time',header:'Время (МСК)',accessorFn:row=>row.simulation?.begins,cell:({getValue})=>moscow(getValue())},
    {id:'account',header:'Аккаунт',accessorFn:row=>[row.step.label,row.step.account].filter(Boolean).join(' · ')||'—'},
    {id:'action',header:'Действие',accessorFn:row=>[labels[row.step.action]||row.step.action,row.step.target,simulationStatus[row.simulation?.status]].filter(Boolean).join(' · ')},
  ]
  return <Card title="Имитация запуска" description={`${steps.length} шагов · ${moscow(plan.simulation.start)} → ${moscow(plan.simulation.finish)} МСК`}>
    <SortableTable columns={columns} data={steps} getRowId={row=>String(row.index)}/>
  </Card>
}

function RunLog({run,disabled,execute}) {
  const log=useRef(null)
  const stay=useRef(true)
  const records=run?.log||[]
  useEffect(()=>{if(log.current&&stay.current)log.current.scrollTop=log.current.scrollHeight},[records.length])
  const active=['running','paused','uncertain'].includes(run?.status)
  return <Card title={<span className="flex items-center gap-2"><Terminal className="size-4"/>Консоль выполнения</span>}
    action={run&&<Badge variant="soft" color={run.status==='running'?'success':run.status==='uncertain'?'warning':'neutral'}>{status[run.status]||run.status}</Badge>}>
    {run?<div className="grid gap-3">
      <p className="text-sm text-muted-foreground">{run.index} / {run.total}{run.status==='running'&&run.due>Date.now()/1000?' · до '+moscow(run.due):''}</p>
      {!!records.length&&<pre className="max-h-[38vh] overflow-auto whitespace-pre-wrap rounded-lg border border-divider bg-background p-3 font-mono text-xs leading-relaxed" ref={log}
        onScroll={()=>{const node=log.current;stay.current=node.scrollHeight-node.scrollTop-node.clientHeight<24}}>
        {records.map(record=>[moscow(record.time,{timeStyle:'medium'}),record.label,record.target,status[record.status],record.note].filter(Boolean).join(' · ')).join('\n')}
      </pre>}
      {active&&<FormActions>
        {run.status==='running'?<Button variant="outline" disabled={disabled} onClick={()=>execute('pause')}>Пауза</Button>:
          <><Button variant="outline" disabled={disabled||run.in_flight} onClick={()=>execute('resume')}>Продолжить</Button><Button variant="outline" disabled={disabled||run.in_flight} onClick={()=>execute('skip')}>Пропустить шаг</Button></>}
        <Button color="destructive" variant="outline" disabled={disabled} onClick={()=>execute('stop')}>Остановить</Button>
      </FormActions>}
    </div>:<Empty>Запусков пока нет.</Empty>}
  </Card>
}

function Wiki({open,setOpen}) {
  const [query,setQuery]=useState('')
  const visible=docs.filter(doc=>doc.join(' ').toLocaleLowerCase('ru').includes(query.trim().toLocaleLowerCase('ru')))
  return <Dialog open={open} onOpenChange={setOpen} title="Вики сценариев" description="Синтаксис, команды Telegram и параметры." size="xl">
    <div className="grid gap-4">
      <Input type="search" label="Найти команду" value={query} onChange={event=>setQuery(event.target.value)} placeholder="Например, views"/>
      {!query&&<pre className="overflow-auto rounded-lg border border-divider bg-muted/40 p-3 font-mono text-xs">{example}</pre>}
      {visible.length?visible.map(([group,syntax,description])=><section key={syntax} className="grid gap-1 border-b border-divider pb-3">
        <span className="text-xs text-muted-foreground">{group}</span><code className="break-words text-sm text-primary-text">{syntax}</code><p className="text-sm">{description}</p>
      </section>):<Empty>Команды не найдены.</Empty>}
    </div>
  </Dialog>
}
