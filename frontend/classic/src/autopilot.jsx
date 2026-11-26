// Monitoring follows the Classic template's KPI row and log panels.
import {useState} from 'react'
import {Activity, Bot, CirclePlay, CircleStop, Plus, Settings2, Users} from 'lucide-react'
import {Badge} from '@dashral/ui/components/badge'
import {BarChart} from '@dashral/ui/components/bar-chart'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import SortableTable from './tables'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {MetricCard} from '@dashral/ui/components/metric-card'
import {Progress} from '@dashral/ui/components/progress'
import {del,moscow,num,post,useAction,useData,url} from './api'
import {Check,Empty,FormActions,Loading,Picker,Problem,TextArea,useConfirm} from './ui'
import {PageHeader} from './page-header'

const actions=[['views','Просмотры'],['reactions','Реакции'],['reposts','Репосты'],['votes','Голоса']]
const emojis=['❤️','👍','👎','🔥','😭','🤬','😡','😍','💔','🤡','💩']
const name=channel=>channel.replace(/^https?:\/\/(?:t\.me|telegram\.me)\//,'@')
const activeBadge=active=><Badge color={active?'success':'neutral'} variant="soft">{active?'Работает':'Остановлен'}</Badge>

export function Autopilot() {
  const monitor=useData('/api/automatic',12000)
  const [selected,setSelected]=useState('all')
  const [postDetail,setPostDetail]=useState(null)
  const [settings,setSettings]=useState(false)
  const [added,setAdded]=useState(false)
  const [link,setLink]=useState('')
  const action=useAction()
  const confirm=useConfirm()
  const channels=monitor.data?.channels||[]
  const channel=channels.find(item=>item.id===selected)
  const channelPath=channel?'/api/automatic/channels/'+url(channel.id):null
  const posts=(channel?[channel]:channels).flatMap(item=>(item.posts||[]).map(post=>({...post,channel: item.channel,channelId:item.id})))
    .sort((left,right)=>Date.parse(right.post_date)-Date.parse(left.post_date))
  const detail=posts.find(post=>post.channelId===postDetail?.channelId&&post.post_id===postDetail?.post_id) || postDetail
  function command(cmd) {
    action.run(async()=>{
      if(cmd.startsWith('stop')&&!await confirm({title:channel?'Остановить мониторинг канала?':'Остановить мониторинг всех каналов?'}))return
      await post(channel?channelPath+'/'+cmd:'/api/automatic/'+cmd+'-all');await monitor.reload()
    })
  }
  const columns=[
    {id:'post',header:'Публикация',accessorFn:item=>'#'+item.post_id+' · '+name(item.channel)+' '+(item.post_text||''),cell:({row})=>
      <button type="button" className="grid max-w-64 cursor-pointer gap-1 text-start hover:text-primary-text" onClick={()=>setPostDetail(row.original)}>
        <span className="font-medium">{name(row.original.channel)} · #{row.original.post_id}</span>
        {row.original.post_text&&<span className="truncate text-xs text-muted-foreground">{row.original.post_text}</span>}
      </button>},
    {id:'date',header:'Дата (МСК)',accessorFn:item=>item.post_date,cell:({getValue})=>moscow(getValue())},
    {id:'mode',header:'График',accessorFn:item=>item.view_mode==='realistic'?'Реалистик':'Легаси'},
    {id:'status',header:'Статус',accessorFn:item=>item.status==='active'?'Активен':'Завершён',cell:({row,getValue})=><Badge variant="soft" color={row.original.status==='active'?'success':'neutral'}>{getValue()}</Badge>},
    ...actions.map(([key,label])=>({id:key,header:label,accessorFn:item=>item['execution_'+key]?.done||0,cell:({row})=>{
      const result=row.original['execution_'+key]
      return result?.target?<span className="tabular-nums">{num(result.done)} / {num(result.target)}</span>:'—'
    }})),
  ]
  return <>
    <PageHeader title="Автопилот" description="Состояние мониторинга, выполнение плана и журнал публикаций.">
      {channel&&<Button variant="outline" onClick={()=>setSettings(true)}><Settings2/>Настройки</Button>}
      {channel ? <Button variant="outline" disabled={action.busy} onClick={()=>command(channel.active?'stop':'start')}>{channel.active?<CircleStop/>:<CirclePlay/>}{channel.active?'Остановить':'Запустить'}</Button> :
        channels.length>0&&<><Button variant="outline" disabled={action.busy} onClick={()=>command('start')}><CirclePlay/>Запустить все</Button>
          <Button variant="outline" disabled={action.busy} onClick={()=>command('stop')}><CircleStop/>Остановить все</Button></>}
    </PageHeader>
    <FormActions>
      <Button size="sm" variant={selected==='all'?'solid':'outline'} onClick={()=>setSelected('all')}>Все каналы</Button>
      {channels.map(item=><Button key={item.id} size="sm" variant={selected===item.id?'solid':'outline'} onClick={()=>setSelected(item.id)}>{name(item.channel)}</Button>)}
      <Button size="sm" variant="outline" onClick={()=>setAdded(true)}><Plus/>Новый канал</Button>
    </FormActions>
    <Problem message={monitor.error||action.error||channel?.error}/>
    {!monitor.data&&!monitor.error?<Loading/>:<>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label={channel?'Мониторинг':'Активные каналы'} value={channel?Number(channel.active):monitor.data?.active_channels??null}
          formatValue={channel?()=>channel.active?'Работает':'Остановлен':undefined} icon={<Bot/>}/>
        <MetricCard label="Активные посты" value={(channel||monitor.data)?.active_posts_count??null} icon={<Activity/>}/>
        <MetricCard label="Доступные аккаунты" value={(channel||monitor.data)?.connected_accounts??null} icon={<Users/>}/>
        <MetricCard label={channel?'Последние просмотры':'Каналы'} value={channel?channel.views??null:monitor.data?.channel_count??null} icon={<Activity/>}/>
      </div>
      {channel&&<Card title={name(channel.channel)} description={channel.view_mode==='realistic'?'Режим «Реалистик»':'Режим «Легаси»'} action={activeBadge(channel.active)}>
        <div className="flex flex-wrap gap-2">
          <Badge variant="soft" color={channel.execute_plan?'success':'neutral'}>{channel.execute_plan?'Автовыполнение':'Наблюдение'}</Badge>
          {channel.subscribers_only&&<Badge variant="soft" color="info">Только подписчики{channel.subscriber_count!=null?' · '+channel.subscriber_count:''}</Badge>}
          <span className="text-sm text-muted-foreground">Последняя проверка {moscow(channel.last_checked)}</span>
        </div>
      </Card>}
      <Card title="Журнал публикаций" description="Откройте пост для просмотра графиков и списка действий.">
        <SortableTable columns={columns} data={posts} getRowId={row=>row.channelId+':'+row.post_id} autoResetPage={false} emptyMessage="Публикаций пока нет"/>
      </Card>
    </>}
    <Dialog open={added} onOpenChange={setAdded} title="Добавить Telegram-канал"
      description="Автопилот начнёт отслеживать новые публикации после добавления."
      form={{onSubmit:event=>{
        event.preventDefault()
        action.run(async()=>{
          const result=await post('/api/automatic/channels',{channel:link.trim()})
          setSelected(result.channels?.at(-1)?.id||'all');setLink('');setAdded(false);await monitor.reload()
        })
      }}}
      footer={<Button type="submit" disabled={action.busy}><Plus/>Добавить</Button>}>
      <div className="grid gap-3"><Input label="Ссылка" placeholder="https://t.me/channel" value={link} onChange={event=>setLink(event.target.value)} required/><Problem message={action.error}/></div>
    </Dialog>
    {settings&&channel&&<ChannelSettings channel={channel} path={channelPath} close={()=>setSettings(false)} refresh={monitor.reload}
      remove={()=>action.run(async()=>{
        if(!await confirm({title:'Удалить канал из Автопилота?',text:name(channel.channel),danger:true,label:'Удалить'}))return
        await del(channelPath);setSelected('all');setSettings(false);await monitor.reload()
      })}/>}
    {detail&&<PostDetail post={detail} close={()=>setPostDetail(null)}/>}
  </>
}

function ChannelSettings({channel,path,close,refresh,remove}) {
  const [form,setForm]=useState({
    view_mode:channel.view_mode||'legacy',max_views:channel.max_views||0,max_reactions:channel.max_reactions||0,
    execute_plan:!!channel.execute_plan,views_enabled:!!channel.views_enabled,subscribers_only:!!channel.subscribers_only,
    allowed_reactions:channel.allowed_reactions||['❤️'],reaction_prompt:channel.reaction_prompt||'',
    view_day2_pct:channel.view_day2_pct??10,
    hours:channel.hours?.join(', ')||'',view_activity:channel.view_activity?.join(', ')||'',
  })
  const [testAt,setTestAt]=useState('')
  const [preview,setPreview]=useState(null)
  const [fullPrompt,setFullPrompt]=useState('')
  const [pollPrompt,setPollPrompt]=useState(false)
  const [roster,setRoster]=useState({count:channel.subscriber_count,checkedAt:channel.subscribers_checked_at})
  const viewLimit=form.subscribers_only ? roster.count : channel.connected_accounts
  const clampViews=value=>value!==''&&viewLimit!=null&&Number(value)>viewLimit?viewLimit:value
  const update=(key,value)=>{
    setPreview(null);setFullPrompt('')
    setForm(state=>{
      const next={...state,[key]:value}
      if(key==='subscribers_only'&&value&&roster.count!=null)next.max_views=Math.min(Number(next.max_views),roster.count)
      return next
    })
  }
  const action=useAction()
  const payload=()=>({...form,max_views:Number(clampViews(form.max_views)),max_reactions:Number(form.max_reactions),view_day2_pct:Number(form.view_day2_pct),
    hours:form.hours.split(',').map(Number),view_activity:form.view_activity.split(',').map(Number)})
  function refreshSubscribers(){
    action.run(async()=>{
      const result=await post(path+'/subscribers')
      const refreshed=result.channels.find(item=>item.id===channel.id)
      if(!refreshed)return
      setRoster({count:refreshed.subscriber_count,checkedAt:refreshed.subscribers_checked_at})
      if(refreshed.subscriber_count!=null)setForm(previous=>previous.subscribers_only?{
        ...previous,max_views:Math.min(Number(previous.max_views),refreshed.subscriber_count),
      }:previous)
      await refresh()
    })
  }
  return <Dialog open title="Настройки канала" description={name(channel.channel)} onOpenChange={open=>{if(!open&&!action.busy)close()}}
    size="xl" form={{onSubmit:event=>{event.preventDefault();action.run(async()=>{await post(path+'/config',payload());await refresh();close()})}}}
    footer={<><Button type="button" variant="ghost" color="destructive" disabled={action.busy} onClick={remove}>Удалить канал</Button><Button type="submit" disabled={action.busy}>Сохранить</Button></>}>
    <div className="grid gap-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <Picker label="График" items={[[ 'legacy','Легаси'],['realistic','Реалистик']]} value={form.view_mode} onValueChange={value=>update('view_mode',value)}/>
        {[['max_views','Цель просмотров'],['max_reactions','Лимит реакций'],['view_day2_pct','Доля второго дня, %']].map(([key,label])=><Input key={key} label={label} type="number" min={0}
          max={key==='view_day2_pct'?100:key==='max_views'?undefined:channel.connected_accounts}
          description={key==='max_views'&&viewLimit!=null?'Максимум: '+viewLimit:undefined}
          value={form[key]} onChange={event=>update(key,key==='max_views'?clampViews(event.target.value):event.target.value)} required/>)}
      </div>
      <div className="flex flex-wrap gap-x-4">
        {[['views_enabled','Добавлять просмотры'],['execute_plan','Выполнять план'],['subscribers_only','Только подписчики']].map(([key,label])=><Check key={key} checked={form[key]} disabled={action.busy} onChange={value=>{
          update(key,value)
          if(key==='subscribers_only'&&value&&roster.count==null)refreshSubscribers()
        }}>{label}</Check>)}
      </div>
      {form.subscribers_only&&<div className="grid gap-2 rounded-lg border border-divider p-3 text-sm">
        <p>{roster.count==null?'Список подписчиков ещё не проверен.':'Подтверждено подписчиков: '+roster.count}
          {roster.checkedAt&&<span className="ms-2 text-muted-foreground">{moscow(roster.checkedAt)} МСК</span>}</p>
        <div><Button type="button" variant="outline" size="sm" disabled={action.busy} onClick={refreshSubscribers}>Обновить список подписчиков</Button></div>
      </div>}
      <details className="rounded-lg border border-divider p-3">
        <summary className="cursor-pointer text-sm font-medium">Почасовой профиль</summary>
        <div className="mt-3 grid gap-3">
          <TextArea label="Легаси · 24 значения через запятую" rows={2} value={form.hours} onChange={event=>update('hours',event.target.value)}/>
          <TextArea label="Реалистик · активность по 24 часам МСК" rows={2} value={form.view_activity} onChange={event=>update('view_activity',event.target.value)}/>
          <div className="flex flex-wrap items-end gap-2">
            <Input type="datetime-local" label="Время тестовой публикации (МСК)" value={testAt} onChange={event=>setTestAt(event.target.value)}/>
            <Button type="button" variant="outline" disabled={action.busy||!testAt} onClick={()=>action.run(async()=>{
              const now=new Date(testAt+'+03:00')
              if(!Number.isFinite(now.getTime()))throw new Error('Укажите дату публикации')
              setPreview(await post(path+'/preview-views',{...payload(),published:now.toISOString(),total:Number(form.max_views)}))
            })}>Показать график</Button>
          </div>
          {preview?.counts?.length>0&&<Hourly data={preview.counts} title="План просмотров" published={preview.published}/>}
        </div>
      </details>
      <div className="grid gap-2">
        <strong className="text-sm">Разрешённые реакции</strong>
        <div className="flex flex-wrap gap-2">
          {[...new Set([...form.allowed_reactions,...emojis])].map(emoji=><Button key={emoji} type="button" size="sm" variant={form.allowed_reactions.includes(emoji)?'solid':'outline'} aria-pressed={form.allowed_reactions.includes(emoji)}
            onClick={()=>update('allowed_reactions',form.allowed_reactions.includes(emoji)?form.allowed_reactions.filter(item=>item!==emoji):[...form.allowed_reactions,emoji])}>{emoji}</Button>)}
        </div>
      </div>
      <TextArea label="Промпт реакций · инструкция канала" rows={6} maxLength={6000} value={form.reaction_prompt} onChange={event=>update('reaction_prompt',event.target.value)}/>
      <div className="grid gap-2">
        <p className="text-sm text-muted-foreground">Инструкция канала добавляется к системному промпту DeepSeek.</p>
        <Check checked={pollPrompt} onChange={value=>{setPollPrompt(value);setFullPrompt('')}}>Промпт для поста с опросом</Check>
        <div><Button type="button" variant="outline" size="sm" disabled={action.busy} onClick={()=>action.run(async()=>{
          if(fullPrompt){setFullPrompt('');return}
          setFullPrompt((await post(path+'/preview-reaction-prompt',{...payload(),poll:pollPrompt})).prompt)
        })}>{fullPrompt?'Скрыть полный промпт':'Показать полный промпт'}</Button></div>
        {fullPrompt&&<>
          <pre className="whitespace-pre-wrap break-words rounded-lg border border-divider bg-background p-3 text-sm">{fullPrompt}</pre>
          <p className="text-xs text-muted-foreground">Для конкретного поста подставляются его лимит и разрешённые реакции. Для встроенного опроса добавляется инструкция для голосования.</p>
        </>}
      </div>
      <Problem message={action.error}/>
    </div>
  </Dialog>
}

function Hourly({data,title,published,observed}) {
  const start=Date.parse(published)
  const rows=(data||[]).map((value,index)=>({
    label:Number.isFinite(start)?moscow((start+index*3600000)/1000,{day:'numeric',hour:'2-digit',minute:'2-digit'}):String(index),
    plan:value,
    actual:observed?.[index],
  }))
  return rows.length?<BarChart data={rows} index="label" height={230} aria-label={title} showLegend={!!observed}
    series={[{key:'plan',label:title,color:'var(--primary)'},...(observed?[{key:'actual',label:'Фактический прирост',color:'var(--success)'}]:[])]}/>:<Empty>Данные графика пока недоступны.</Empty>
}
function PostDetail({post,close}) {
  const [metric,setMetric]=useState('views')
  const chart=metric==='views'?['model_view_hours','observed_view_hours','План просмотров']:['model_reaction_hours','observed_reaction_hours','План реакций']
  return <Dialog open onOpenChange={open=>{if(!open)close()}} size="xl" title={name(post.channel)+' · #'+post.post_id} description={moscow(post.post_date)+' МСК'}>
    <div className="grid gap-4">
      {post.post_text&&<p className="max-h-32 overflow-auto whitespace-pre-wrap text-sm">{post.post_text}</p>}
      <div className="grid gap-2 sm:grid-cols-2">
        {actions.map(([key,label])=>{
          const action=post['execution_'+key]
          return action?.target>0&&<div key={key} className="grid gap-2 rounded-lg border border-divider p-3 text-sm">
            <span className="flex justify-between gap-2">{label}<b>{action.done}/{action.target}</b></span>
            <Progress value={100*action.done/action.target} aria-label={label}/>
            <span className="text-xs text-muted-foreground">{action.next_due_at?'Следующее: '+moscow(action.next_due_at):'В ожидании: '+action.pending} · ошибок: {action.errors}</span>
          </div>
        })}
      </div>
      <Picker items={actions.slice(0,2)} value={metric} onValueChange={setMetric} aria-label="График"/>
      <Hourly data={post[chart[0]]} observed={post[chart[1]]} title={chart[2]} published={post.post_date}/>
      {post.poll&&<Card title={post.poll.question} description={post.poll.open?'Опрос открыт':'Опрос закрыт'}>
        <ul className="grid gap-2 text-sm">{post.poll.options.map((option,index)=><li key={index} className="flex justify-between gap-2">{option.text}<b>{option.done}/{option.planned}</b></li>)}</ul>
      </Card>}
      {post.suggestion?.message&&<p className="text-sm text-muted-foreground">{post.suggestion.message}</p>}
    </div>
  </Dialog>
}
