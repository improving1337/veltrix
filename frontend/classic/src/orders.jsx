import {useEffect,useMemo,useRef,useState} from 'react'
import {ArrowLeft, CirclePlay, ClipboardList, Clock3, Plus, Trash2} from 'lucide-react'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import SortableTable from './tables'
import {Input} from '@dashral/ui/components/input'
import {LineChart} from '@dashral/ui/components/line-chart'
import {Progress} from '@dashral/ui/components/progress'
import {api,flags,moscow,post,useAction} from './api'
import {Check,DownloadButton,Empty,FormActions,Loading,Picker,Problem,TextArea,useConfirm} from './ui'
import {PageHeader} from './page-header'
import {services} from './overview'

const serviceChoices=Object.entries(services)
const emojis=['❤️','👍','👎','🔥','😭','🤬','😍','💔','🤡','💩','🎉','😁','👏','🤩','💯','👌','🙏','random']
const statusLabels={running:'В работе',done:'Готово',stopped:'Остановлен',queue:'В очереди',wait:'Ожидает',active:'Выполняется',ok:'Выполнено',err:'Ошибка',skip:'Пропущено'}
const confirmed=job=>job.items.filter(item=>item.state==='ok').length
const statusBadge=status=><Badge variant="soft" color={status==='done'||status==='ok'?'success':status==='err'?'destructive':status==='running'?'info':'neutral'}>{statusLabels[status]||status}</Badge>
const orderKey=job=>job.service+':'+job.id
const formDefaults={link:'',count:'1',delayFrom:'10',delayTo:'30',emoji:'❤️',reactionPercent:'100',reactionChance:'30',channels:'',postLinks:'',unique:false,freshness:'0',mode:'intervals',minutes:'120',boost:'70',finish:false,deadline:'',quiet:false,from:'',to:'',filters:[],join:'and'}

function useOrders() {
  const [state,setState]=useState({data:[],loading:true,error:''})
  const counter=useRef(0)
  async function reload() {
    const id=++counter.current
    const results=await Promise.allSettled(serviceChoices.map(([key])=>api('/api/'+key+'/job')))
    if(id!==counter.current)return
    const jobs=results.flatMap((result,index)=>result.status==='fulfilled'?(result.value?.jobs||[]).map(job=>({...job,service:serviceChoices[index][0]})):[])
    setState({data:jobs.sort((a,b)=>(b.seq||0)-(a.seq||0)),loading:false,error:results.every(result=>result.status==='rejected')?'Не удалось загрузить заказы.':''})
  }
  useEffect(()=>{
    reload()
    const interval=setInterval(()=>{if(!document.hidden)reload()},4000)
    return ()=>{++counter.current;clearInterval(interval)}
  },[])
  return {...state,reload}
}

export function Orders() {
  const orders=useOrders()
  const [form,setForm]=useState(false)
  const [selection,setSelection]=useState('')
  const detail=orders.data.find(order=>orderKey(order)===selection)
  if(form)return <OrderForm cancel={()=>setForm(false)} started={(id,service)=>{setForm(false);setSelection(service+':'+id);orders.reload()}}/>
  if(detail)return <JobDetails order={detail} back={()=>setSelection('')} reload={orders.reload}/>
  const columns=[
    {id:'id',header:'Заказ',accessorFn:order=>order.seq??order.id,cell:({row})=><Button size="sm" variant="ghost" className="font-semibold" onClick={()=>setSelection(orderKey(row.original))}>#{row.original.seq||row.original.id.slice(0,7)}</Button>},
    {id:'service',header:'Услуга',accessorFn:job=>services[job.service]||job.service},
    {accessorKey:'link',header:'Ссылка'},
    {accessorKey:'status',header:'Статус',cell:({getValue})=>statusBadge(getValue())},
    {id:'progress',header:'Выполнение',accessorFn:confirmed,cell:({row})=>{
      const total=row.original.items.length,done=confirmed(row.original)
      return <div className="grid min-w-20 gap-1"><span>{done}/{total}</span><Progress value={100*done/(total||1)} aria-label="Прогресс заказа"/></div>
    }},
    {id:'started',header:'Запуск',accessorFn:job=>job.started,cell:({getValue})=>moscow(getValue())},
  ]
  return <>
    <PageHeader title="Услуги" description="Запланируйте заказ и отслеживайте выполнение по аккаунтам.">
      <Button onClick={()=>setForm(true)}><Plus/>Новый заказ</Button>
    </PageHeader>
    <Problem message={orders.error}/>
    <Card title="Заказы" description={`В работе: ${orders.data.filter(job=>job.status==='running').length}`}>
      {orders.loading?<Loading/>:<SortableTable columns={columns} data={orders.data} getRowId={orderKey} autoResetPage={false} searchPlaceholder="Поиск заказов" emptyMessage="Заказов пока нет"/>}
    </Card>
  </>
}

function JobDetails({order,back,reload}) {
  const action=useAction()
  const confirm=useConfirm()
  const total=order.items.length,done=confirmed(order),running=order.status==='running'
  function command(verb) {
    action.run(async()=>{
      if(verb==='stop'&&!await confirm({title:'Остановить заказ?',text:'Оставшиеся аккаунты будут сняты с очереди.',danger:true}))return
      await post('/api/'+order.service+'/'+verb,{job_id:order.id});await reload()
    })
  }
  const columns=[
    {accessorKey:'account',header:'Аккаунт'},
    {accessorKey:'state',header:'Статус',cell:({getValue})=>statusBadge(getValue())},
    {accessorKey:'note',header:'Результат'},
    {accessorKey:'wait_left',header:'Ожидание, с',cell:({getValue})=>getValue()||'—'},
    {accessorKey:'finished',header:'Завершён',cell:({getValue})=>moscow(getValue())},
  ]
  return <>
    <PageHeader title={`Заказ #${order.seq||order.id.slice(0,7)}`} description={`${services[order.service]} · ${order.link}`}>
      <Button variant="outline" onClick={back}><ArrowLeft/>Заказы</Button>
    </PageHeader>
    <Card title={`${done} из ${total} успешно`} action={statusBadge(order.status)}>
      <Progress value={100*done/(total||1)} aria-label="Прогресс"/>
      {running&&<div className="mt-4"><FormActions><Button variant="outline" disabled={action.busy} onClick={()=>command('skip')}>Ускорить текущий шаг</Button><Button variant="outline" color="destructive" disabled={action.busy} onClick={()=>command('stop')}>Остановить</Button></FormActions></div>}
    </Card>
    <Problem message={action.error}/>
    <Card title="Аккаунты заказа"><SortableTable columns={columns} data={order.items} getRowId={item=>item.account} autoResetPage={false}/></Card>
  </>
}

function timeValue(value) {
  const digits=value.replace(/\D/g,'').slice(0,4)
  return digits.length>2?digits.slice(0,2)+':'+digits.slice(2):digits
}
function OrderForm({cancel,started}) {
  const [service,setService]=useState('views')
  const [form,setForm]=useState(formDefaults)
  const [plans,setPlans]=useState(null)
  const [poll,setPoll]=useState(null)
  const [votes,setVotes]=useState({})
  const action=useAction()
  const confirm=useConfirm()
  const subentry=service==='subentry'
  const endpoint='/api/'+service
  const activePlan=useRef(null)
  const alive=useRef(true)
  useEffect(()=>{alive.current=true;return ()=>{
    alive.current=false
    const current=activePlan.current
    if(current)post(current.path+'/cancel',{plan_id:current.id}).catch(()=>{})
  }},[])
  function discard() {
    const current=activePlan.current
    if(!current)return
    activePlan.current=null
    setPlans(null)
    post(current.path+'/cancel',{plan_id:current.id}).catch(()=>{})
  }
  function update(key,value) {
    discard()
    if(key==='link'){setPoll(null);setVotes({})}
    setForm(previous=>({...previous,[key]:value}))
  }
  function extras(forSchedule=false) {
    const extra={account_filters:form.filters.map(rule=>({...rule,value:rule.field==='flag'?rule.value:rule.value===''?null:Number(rule.value)})),filter_mode:form.join,freshness_days:form.freshness||0}
    if(service==='reactions'||service==='multi'||subentry)extra.emoji=form.emoji
    if(service==='multi')extra.reaction_percent=Number(form.reactionPercent)
    if(subentry){
      extra.post_links=form.postLinks.split(/[\s,;]+/).filter(Boolean)
      extra.reaction_percent=Number(form.reactionChance)
    }
    if(service==='multi'||service==='btn'||subentry)extra.channels=form.channels.split(/[\s,;]+/).filter(Boolean)
    if(service==='multi')extra.unique=form.unique
    if(service==='polls')extra.votes=votes
    if(!forSchedule)return extra
    if(form.finish) {
      extra.finish_at=Date.parse(form.deadline+'+03:00')/1000
      if(!Number.isFinite(extra.finish_at)||extra.finish_at<=Date.now()/1000)throw new Error('Укажите будущий срок по МСК')
    }
    if(form.quiet) {
      const time=/^(?:[01]\d|2[0-3]):[0-5]\d$/
      if(!time.test(form.from)||!time.test(form.to)||form.from===form.to)throw new Error('Укажите два разных времени пропуска ЧЧ:ММ')
      extra.quiet_hours={from:form.from,to:form.to}
    }
    if(form.mode==='realistic')extra.realistic={minutes:form.finish?120:Number(form.minutes),boost:Number(form.boost)}
    return extra
  }
  function preview(event) {
    event.preventDefault()
    action.run(async()=>{
      discard()
      const spread=form.mode==='intervals'&&!form.finish
      const plan=await post(endpoint+'/plan',{
        link:form.link.trim(),count:form.count,delay_from:spread?form.delayFrom:0,delay_to:spread?form.delayTo:0,extra:extras(true),
      })
      if(!alive.current){post(endpoint+'/cancel',{plan_id:plan.id}).catch(()=>{});return}
      activePlan.current={id:plan.id,path:endpoint}
      setPlans({...plan,at:Date.now()/1000})
    })
  }
  function ruleChange(index,fragment) {
    update('filters',form.filters.map((rule,i)=>i===index?{...rule,...fragment}:rule))
  }
  const valueInput=(key,label,props={})=><Input key={key} label={label} value={form[key]} onChange={event=>update(key,event.target.value)} {...props}/>
  return <>
    <PageHeader title="Новый заказ" description="Сначала постройте план, затем подтвердите запуск.">
      <Button variant="outline" onClick={cancel}><ArrowLeft/>Заказы</Button>
    </PageHeader>
    <Card title="Параметры заказа">
      <form className="grid gap-4" onSubmit={preview}>
        <div className="grid gap-3 md:grid-cols-2">
          <Picker label="Услуга" items={serviceChoices} value={service} onValueChange={next=>{discard();setPoll(null);setVotes({});setService(next)}}/>
          {!subentry&&valueInput('link','Ссылка Telegram',{placeholder:'https://t.me/channel/123',required:true})}
        </div>
        {subentry&&<div className="grid gap-3 md:grid-cols-2">
          <TextArea label="Каналы для подписки" rows={4} required placeholder="По одной ссылке на строку" value={form.channels} onChange={event=>update('channels',event.target.value)}/>
          <TextArea label="Посты с кнопками" rows={4} required placeholder="По одной ссылке на пост на строку" value={form.postLinks} onChange={event=>update('postLinks',event.target.value)}/>
        </div>}
        {(service==='reactions'||service==='multi'||subentry)&&<div className="grid gap-3 md:grid-cols-2">
          <Picker label="Реакция" items={emojis.map(value=>[value,value==='random'?'Случайная':value])} value={form.emoji}
            disabled={(service==='multi'&&form.reactionPercent==='0')||(subentry&&form.reactionChance==='0')} onValueChange={value=>update('emoji',value)}/>
          {service==='multi'&&valueInput('reactionPercent','Аккаунтов с реакцией, %',{
            type:'number',min:0,max:100,step:1,required:true,description:'0% — без реакций. При 50% реакцию ставит примерно каждый второй аккаунт.',
          })}
          {subentry&&valueInput('reactionChance','Шанс реакции на каждый пост, %',{type:'number',min:0,max:100,step:1,required:true})}
        </div>}
        {['multi','btn'].includes(service)&&<TextArea label="Каналы для подписки" rows={2} value={form.channels} onChange={event=>update('channels',event.target.value)} placeholder="@channel, https://t.me/another"/>}
        {service==='multi'&&<Check checked={form.unique} onChange={value=>update('unique',value)}>Только уникальные аккаунты</Check>}
        <div className="grid gap-3 md:grid-cols-2">
          {valueInput('freshness','Минимум дней после смены аватарки',{type:'number',min:0,max:36500})}
          {service!=='polls'&&<div className="flex items-end gap-2">
            {valueInput('count','Количество аккаунтов',{type:'number',min:1,required:true,className:'w-full'})}
            <Button type="button" variant="outline" disabled={action.busy||(subentry?!form.channels.trim()||!form.postLinks.trim():!form.link.trim())} onClick={()=>action.run(async()=>{
              const result=await post(endpoint+'/available',{link:form.link.trim(),extra:extras()});update('count',String(result.count))
            })}>Макс.</Button>
          </div>}
        </div>
        {service==='polls'&&<div className="grid gap-3">
          <div><Button type="button" variant="outline" disabled={action.busy||!form.link.trim()} onClick={()=>action.run(async()=>{
            discard();setPoll(await post('/api/polls/options',{link:form.link.trim()}));setVotes({})
          })}>Узнать варианты</Button></div>
          {poll&&<><strong>{poll.question}</strong><div className="grid gap-3 sm:grid-cols-2">{poll.options.map(option=>
            <Input key={option.id} label={option.text} type="number" min={0} value={votes[option.id]??''} onChange={event=>{discard();setVotes(previous=>({...previous,[option.id]:Number(event.target.value)}))}}/>)}</div></>}
        </div>}
        <details open={form.filters.length>0?true:undefined} className="rounded-md border border-divider p-3">
          <summary className="cursor-pointer text-sm font-medium">Фильтры аккаунтов</summary>
          <div className="mt-3 grid gap-3">
            {!!form.filters.length&&<Picker label="Объединение" items={[[ 'and','Все условия (И)'],['or','Любое условие (ИЛИ)']]} value={form.join} onValueChange={value=>update('join',value)}/>}
            {form.filters.map((rule,index)=><div key={index} className="grid items-end gap-2 sm:grid-cols-[2fr_1fr_2fr_auto]">
              <Picker label="Поле" items={[[ 'avatar_count','Аватарки'],['music_count','Музыка'],['flag','Метка']]} value={rule.field} onValueChange={field=>ruleChange(index,{field,op:field==='flag'?'=':'>=',value:field==='flag'?'public':1})}/>
              <Picker label="Оператор" items={(rule.field==='flag'?['=']:['>','>=','=','<','<=']).map(item=>[item,item])} value={rule.op} onValueChange={op=>ruleChange(index,{op})}/>
              {rule.field==='flag'?<Picker label="Значение" items={Object.entries(flags).filter(([key])=>key!=='autopilot')} value={rule.value} onValueChange={value=>ruleChange(index,{value})}/>:
                <Input label="Значение" type="number" min={0} required value={rule.value} onChange={event=>ruleChange(index,{value:event.target.value})}/>}
              <Button type="button" variant="outline" size="icon" aria-label="Удалить условие" onClick={()=>update('filters',form.filters.filter((_,i)=>i!==index))}><Trash2/></Button>
            </div>)}
            <div><Button type="button" variant="outline" disabled={form.filters.length>=12} onClick={()=>update('filters',[...form.filters,{field:'avatar_count',op:'>=',value:1}])}><Plus/>Добавить условие</Button></div>
          </div>
        </details>
        <Picker label="Расписание" items={[[ 'intervals','Интервалы'],['realistic','Реалистик']]} value={form.mode} onValueChange={value=>update('mode',value)}/>
        <div className="grid gap-3 sm:grid-cols-2">
          <Check checked={form.finish} onChange={value=>update('finish',value)}>Закончить к</Check>
          <Check checked={form.quiet} onChange={value=>update('quiet',value)}>Время пропуска</Check>
          {form.mode==='realistic'&&<>
            {valueInput('minutes','Длительность, минут',{type:'number',min:1,max:2880,disabled:form.finish,required:!form.finish})}
            {valueInput('boost','Сила роста в начале',{type:'number',min:0,max:100,required:true})}
          </>}
          {form.mode==='intervals'&&!form.finish&&<>
            {valueInput('delayFrom','Задержка от, с',{type:'number',min:0,required:true})}
            {valueInput('delayTo','Задержка до, с',{type:'number',min:0,required:true})}
          </>}
          {form.finish&&valueInput('deadline','Закончить к (МСК)',{type:'datetime-local',required:true})}
        </div>
        {form.quiet&&<div className="grid gap-3 sm:grid-cols-2">
          {[['from','Пропуск с (МСК)'],['to','Пропуск до (МСК)']].map(([key,label])=><Input key={key} label={label} inputMode="numeric" placeholder="ЧЧ:ММ" value={form[key]} onChange={event=>update(key,timeValue(event.target.value))} required/>)}
        </div>}
        <Problem message={action.error}/>
        <div><Button type="submit" disabled={action.busy||(service==='polls'&&!poll)}><Clock3/>Построить план</Button></div>
      </form>
    </Card>
    {plans&&<OrderPlan plan={plans} disabled={action.busy} cancel={discard}
      exclude={account=>action.run(async()=>{const next=await post(endpoint+'/exclude',{plan_id:plans.id,account});setPlans({...next,at:Date.now()/1000})})}
      submit={()=>action.run(async()=>{
        if(!await confirm({title:'Запустить заказ?',text:`${services[service]} · ${plans.items.length} аккаунтов`,label:'Запустить'}))return
        const result=await post(endpoint+'/start',{plan_id:plans.id})
        activePlan.current=null
        started(result.job.id,service)
      })}/>}
  </>
}

function OrderPlan({plan,exclude,submit,cancel,disabled}) {
  const [zoom,setZoom]=useState(1)
  const [text,setText]=useState(false)
  const duration=Math.max(1,...plan.items.map(item=>item.at))
  const timeline=useMemo(()=>{
    let count=0
    return Array.from({length:91},(_,index)=>{
      const seconds=duration*index/90
      while(count<plan.items.length&&plan.items[count].at<=seconds+1e-9)++count
      return {label:moscow(plan.at+seconds,{hour:'2-digit',minute:'2-digit',second:duration<600?'2-digit':undefined}),count}
    })
  },[plan,duration])
  const hasReactions = plan.reaction_count != null
  const columns=[
    {accessorKey:'account',header:'Аккаунт'},
    {accessorKey:'username',header:'Юзернейм'},
    {id:'time',accessorFn:item=>item.at,header:'Время (МСК)',cell:({getValue})=>moscow(plan.at+getValue())},
    ...(!hasReactions?[]:[{id:'reaction',header:'Реакция',
      accessorFn:item=>item.reaction_planned?'Да':'—'}]),
    {id:'remove',header:'',cell:({row})=><Button size="icon-sm" variant="ghost" aria-label="Исключить аккаунт" disabled={disabled||plan.items.length===1} onClick={()=>exclude(row.original.account)}><Trash2/></Button>},
  ]
  return <Card title="Предпросмотр расписания" description={`${plan.items.length} аккаунтов · до ${moscow(plan.at+duration)} МСК`}
    action={<FormActions><Button variant="outline" onClick={()=>setText(value=>!value)}><ClipboardList/>{text?'Скрыть план':'План текстом'}</Button>
      <Button disabled={disabled} onClick={submit}><CirclePlay/>Подтвердить и запустить</Button>
      <Button variant="ghost" onClick={cancel} disabled={disabled}>Отмена</Button></FormActions>}>
    <div className="grid gap-4">
      <label className="flex items-center gap-3 text-sm">Масштаб <input type="range" min={1} max={8} value={zoom} onChange={event=>setZoom(+event.target.value)}/></label>
      <div className="overflow-x-auto"><div style={{width:100*zoom+'%'}}><LineChart index="label" data={timeline} series={[{key:'count',label:'Аккаунтов',color:'var(--primary)'}]} height={260} curve="step" aria-label="Аккаунты по времени"/></div></div>
      {hasReactions&&<p className="text-sm text-muted-foreground">
        Реакция по плану у {plan.reaction_count} из {plan.items.length} аккаунтов · задано {plan.reaction_percent}%
      </p>}
      {plan.reaction_chance!=null&&<section className="grid gap-2 text-sm">
        <p>Шанс реакции на каждый пост {plan.reaction_chance}%</p>
        <ol className="list-decimal space-y-1 ps-5">{plan.steps.map((step,index)=><li key={index}>{step}</li>)}</ol>
      </section>}
      {!!plan.already?.length&&<p className="text-sm text-muted-foreground">Уже выполнили действие: {plan.already.length}</p>}
      {text&&<><div><DownloadButton filename="veltrix-plan.csv" rows={[[ 'Время МСК','Аккаунт','Юзернейм',...(hasReactions?['Реакция']:[])],
        ...plan.items.map(item=>[moscow(plan.at+item.at),item.account,item.username,...(hasReactions?[item.reaction_planned?'Да':'—']:[])])]}/></div>
        <SortableTable columns={columns} data={plan.items} getRowId={item=>item.account}/></>}
    </div>
  </Card>
}
