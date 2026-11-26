import {useEffect,useState} from 'react'
import {ArrowLeft, CircleDollarSign, Coins, ExternalLink, GitBranch, RefreshCw, Trophy} from 'lucide-react'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import SortableTable from './tables'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {MetricCard} from '@dashral/ui/components/metric-card'
import {api,connected,fullName,moscow,num,post,useAction,useData,url} from './api'
import {AccountAvatar,AccountPicker,Check,Empty,FormActions,Loading,Picker,Problem,useConfirm} from './ui'
import {PageHeader} from './page-header'
import {History} from './overview'

const root='/api/cs3bet'
const bulkPath=root+'/bulk'
const gameStatuses={queue:'Очередь',checking:'Проверка',sending:'Отправка',ok:'Принято',unknown:'Нужно проверить',error:'Ошибка до отправки',not_run:'Не выполнено',skipped:'Пропущено',rejected:'Отклонено',partial:'Часть принята'}
const resumable=new Set(['error','not_run','skipped','rejected'])
export function Cs3bet({accounts}) {
  const [view,setView]=useState('accounts')
  const [target,setTarget]=useState('')
  const [hideEmpty,setHideEmpty]=useState(false)
  const [promo,setPromo]=useState(false)
  const overview=useData('/api/overview',30000)
  const state=useData(root+'/state',5000)
  const template=useData(root+'/template')
  const users=connected(accounts).filter(account=>account.flag!=='autopilot')
  const action=useAction()
  const user=users.find(account=>account.name===target)
  function open(view,name=users[0]?.name||'') {setTarget(name);setView(view)}
  function miniApp(account) {
    const popup=window.open('about:blank','_blank')
    if(!popup){action.setError('Разрешите всплывающие окна');return}
    popup.opener=null
    action.run(async()=>{
      try {
        const result=await post(root+'/open/'+url(account.name))
        if(String(result.account_id)!==String(account.id)||!result.url?.startsWith('https://'))throw new Error('Telegram не вернул ссылку для выбранного аккаунта')
        popup.location.replace(result.url)
      }catch(error){popup.close();throw error}
    })
  }
  const rows=users.map(account=>({...account,game:state.data?.items?.[account.name]||{}})).filter(account=>!hideEmpty||account.game.balance==null||Number(account.game.balance)!==0)
  const columns=[
    {id:'account',header:'Аккаунт',accessorFn:fullName,cell:({row})=><button className="flex cursor-pointer items-center gap-2 text-start font-medium hover:text-primary-text" onClick={()=>open('bet',row.original.name)}><AccountAvatar account={row.original}/>{fullName(row.original)}</button>},
    {id:'username',header:'Username',accessorFn:account=>account.username?'@'+account.username:'—'},
    {id:'balance',header:'Баланс',accessorFn:account=>account.game.balance==null?undefined:Number(account.game.balance),sortUndefined:'last',cell:({getValue})=>num(getValue())},
    {id:'checked',header:'Проверено (МСК)',accessorFn:account=>account.game.ts,cell:({getValue})=>moscow(getValue())},
    {id:'actions',header:'Действия',cell:({row})=><FormActions><Button variant="outline" size="sm" onClick={()=>open('bet',row.original.name)}>Ставка</Button>
      <Button variant="outline" size="sm" disabled={action.busy} title="Открыть Mini App" onClick={()=>miniApp(row.original)}><ExternalLink/>Mini App</Button></FormActions>},
  ]
  if(view==='bulk')return <BulkBets users={users} selected={target} setSelected={setTarget} back={()=>setView('accounts')}/>
  if(view==='bet'||view==='template')return <BetDesk key={view+target} view={view} users={users} selected={user} template={template} change={setTarget}
    back={()=>setView('accounts')} refresh={state.reload}/>
  return <>
    <PageHeader title="CS3BET" description="Виртуальные монеты, счета и ставки аккаунтов.">
      <Button variant="outline" disabled={!users.length} onClick={()=>setPromo(true)}>Промокод</Button>
      <Button variant="outline" disabled={!users.length} onClick={()=>open('template')}>Шаблон</Button>
      <Button disabled={!users.length} onClick={()=>open('bulk')}><GitBranch/>Вилка</Button>
    </PageHeader>
    <Problem message={action.error||overview.error||state.error}/>
    <div className="grid gap-4 sm:grid-cols-2">
      <MetricCard label="Общий баланс" value={overview.data?.balance??null} icon={<Coins/>} locale="ru-RU"/>
      <MetricCard label="Наибольший баланс" value={overview.data?.largest??null} icon={<Trophy/>} locale="ru-RU"/>
    </div>
    <History data={overview.data} type="balance"/>
    <Card title="Аккаунты CS3BET" action={<Button variant="outline" size="sm" onClick={state.reload}><RefreshCw/>Обновить</Button>}>
      <div className="mb-3"><Check checked={hideEmpty} onChange={setHideEmpty}>Скрыть нулевой баланс</Check></div>
      {state.data?<SortableTable columns={columns} data={rows} getRowId={account=>account.name} autoResetPage={false} emptyMessage="Нет подключённых аккаунтов"/>:!state.error&&<Loading/>}
    </Card>
    {promo&&<Promo users={users} close={()=>setPromo(false)}/>}
  </>
}

function BetDesk({view,users,selected,template,change,back,refresh}) {
  const name=selected?.name
  const path=name?'/'+url(name):null
  const matches=useData(path?root+'/matches'+path:null)
  const [choices,setChoices]=useState({})
  const [stake,setStake]=useState('')
  const [note,setNote]=useState('')
  const [extra,setExtra]=useState('')
  const action=useAction()
  const confirm=useConfirm()
  const templateMode=view==='template'
  const rewards=useData(extra==='rewards'&&path?root+'/rewards'+path:null)
  const expresses=useData(extra==='stats'&&path?root+'/express-stats'+path:null)
  useEffect(()=>{
    if(templateMode&&template.data)setChoices(Object.fromEntries(template.data.legs.map(leg=>[leg.match_id,leg.side])))
  },[templateMode,template.data])
  const market=matches.data?.items||[]
  const legs=market.filter(match=>choices[match.id]).map(match=>({
    match_id:match.id,side:choices[match.id],team_a:match.team_a,team_b:match.team_b,odds:choices[match.id]==='1'?match.odds_a:match.odds_b,
  }))
  const total=legs.reduce((value,leg)=>value*Number(leg.odds),1)
  const ready=legs.length>0&&total>1&&total<=30
  function submit(useBalance=false) {
    action.run(async()=>{
      if(!templateMode&&!await confirm({title:'Отправить ставку?',text:`${fullName(selected)} · ${useBalance?'доступный баланс, не более 50 000':num(stake)} монет · ×${num(total)}`}))return
      if(templateMode) {
        await post(root+'/template',{legs});await template.reload();setNote('Шаблон сохранён')
      } else {
        const result=await post(root+'/bet'+path,{stake:useBalance?'all':stake,legs})
        if(!result.ok)throw new Error(result.error||'Ставка не принята')
        setNote(`Ставка принята · ×${num(result.coefficient)} · выплата до ${num(result.potential)}`);setChoices({});refresh()
      }
    })
  }
  return <>
    <PageHeader title={templateMode?'Шаблон ставок':'Новая ставка'} description={selected?fullName(selected):'Выберите аккаунт'}>
      <Button variant="outline" onClick={back}><ArrowLeft/>CS3BET</Button>
      <Button variant="outline" disabled={!path} onClick={matches.reload}><RefreshCw/>Матчи</Button>
    </PageHeader>
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
      <Card title="Доступные матчи" description="Выберите по одному исходу в каждом матче.">
        <div className="grid gap-3">
          <AccountPicker items={users} value={name||''} onChange={change}/>
          <Problem message={matches.error}/>
          {!matches.data&&!matches.error&&path?<Loading/>:!market.length&&<Empty>Матчей нет.</Empty>}
          {market.map(match=><div key={match.id} className="grid gap-2 rounded-lg border border-divider p-3">
            <div className="flex items-center justify-between gap-2 text-sm"><strong>{match.team_a} — {match.team_b}</strong>
              <Badge variant="soft" color={match.status==='betting_open'?'success':'neutral'}>{match.status==='betting_open'?'Открыт':'Закрыт'}</Badge></div>
            <div className="grid grid-cols-2 gap-2">{[['1',match.team_a,match.odds_a],['2',match.team_b,match.odds_b]].map(([side,team,odds])=>
              <Button key={side} variant={choices[match.id]===side?'solid':'outline'} disabled={match.status!=='betting_open'||odds==null}
                onClick={()=>setChoices(previous=>{
                  const next={...previous}
                  if(next[match.id]===side)delete next[match.id];else next[match.id]=side
                  return next
                })}>{team} · ×{num(odds)}</Button>)}</div>
          </div>)}
        </div>
      </Card>
      <div className="grid content-start gap-4">
        <Card title={templateMode?'Шаблон':'Купон'} description={legs.length?`${legs.length} исходов · ×${num(total)}`:'Выберите исходы'}>
          <div className="grid gap-3">
            {!templateMode&&<>
              <Input label="Сумма, игровых монет" type="number" min={100} max={50000} step="0.01" value={stake} onChange={event=>setStake(event.target.value)}/>
              {ready&&Number(stake)>=100&&<p className="text-sm text-muted-foreground">Возможная выплата {num(Number(stake)*total)}</p>}
              <Button variant="ghost" disabled={!template.data?.legs?.length} onClick={()=>setChoices(Object.fromEntries(template.data.legs.map(leg=>[leg.match_id,leg.side])))}>Загрузить шаблон</Button>
            </>}
            <Problem message={action.error}/>
            {note&&<p className="text-sm text-success-text" role="status">{note}</p>}
            <FormActions>
              <Button disabled={action.busy||(templateMode?!matches.data:!ready||Number(stake)<100||Number(stake)>50000)} onClick={()=>submit()}>{templateMode?'Сохранить шаблон':'Подтвердить ставку'}</Button>
              {!templateMode&&<Button variant="outline" disabled={!ready||action.busy} onClick={()=>submit(true)}>На доступный баланс</Button>}
            </FormActions>
          </div>
        </Card>
        {!templateMode&&path&&<Card title="Данные аккаунта">
          <FormActions><Button variant="outline" size="sm" onClick={()=>setExtra('rewards')}>Награды</Button><Button variant="outline" size="sm" onClick={()=>setExtra('stats')}>Экспрессы</Button></FormActions>
          <div className="mt-3"><Problem message={rewards.error||expresses.error}/></div>
          {extra==='stats'&&expresses.data&&<p className="text-sm text-muted-foreground">Активные экспрессы: {expresses.data.count??'—'} · возможная выплата: {num(expresses.data.potential)}</p>}
          {extra==='rewards'&&rewards.data&&<Rewards path={path} data={rewards.data} refresh={rewards.reload}/>}
        </Card>}
      </div>
    </div>
  </>
}

function Rewards({path,data,refresh}) {
  const action=useAction()
  const [note,setNote]=useState('')
  const entries=[['daily','Дневная',data.state?.daily?.claimable],['wheel','Колесо',data.state?.wheel?.available]]
  return <div className="grid gap-2 pt-3">
    {entries.map(([key,label,available])=><div className="flex flex-wrap items-center justify-between gap-2 text-sm" key={key}><span>{label}</span>
      <Button variant="outline" size="sm" disabled={action.busy||!available} onClick={()=>action.run(async()=>{
        const result=await post(root+'/rewards'+path+'/collect',{[key]:true})
        if(result.error||result.rate_limited)throw new Error(result.error||'Достигнут лимит запросов')
        setNote(Object.values(result.results||{}).map(item=>item.message||item.text||String(item)).join(' · ')||'Проверка завершена')
        await refresh()
      })}>Получить</Button>
    </div>)}
    <Problem message={action.error||data.error}/>{note&&<p className="text-xs text-muted-foreground">{note}</p>}
  </div>
}

function Promo({users,close}) {
  const [account,setAccount]=useState('')
  const [all,setAll]=useState(true)
  const [code,setCode]=useState('')
  const [result,setResult]=useState([])
  const action=useAction()
  const confirm=useConfirm()
  return <Dialog open title="Промокод CS3BET" onOpenChange={open=>{if(!open&&!action.busy)close()}}
    form={{onSubmit:event=>{
      event.preventDefault()
      action.run(async()=>{
        const selected=all?users:users.filter(item=>item.name===account)
        if(!selected.length)throw new Error('Выберите аккаунт')
        if(!await confirm({title:'Активировать промокод?',text:`Аккаунтов: ${selected.length}`}))return
        setResult([])
        for(const user of selected) {
          try {
            const result=await post(root+'/promo/'+url(user.name),{code:code.trim()})
            setResult(rows=>[...rows,fullName(user)+' · '+(result.message||(result.ok?'Принят':'Отклонён'))])
            if(result.rate_limited) {
              action.setError('Достигнут лимит. После паузы повторите запуск с тем же кодом: уже активированные аккаунты будут пропущены.')
              break
            }
          }catch(error){setResult(rows=>[...rows,fullName(user)+' · '+error.message])}
        }
      })
    }}} footer={<Button type="submit" disabled={action.busy||(!all&&!account)||!code.trim()}>Активировать</Button>}>
    <div className="grid gap-3">
      <Check checked={all} onChange={setAll} disabled={action.busy}>На все доступные аккаунты ({users.length})</Check>
      {!all&&<AccountPicker items={users} value={account} onChange={setAccount}/>}
      <Input label="Промокод" value={code} onChange={event=>setCode(event.target.value)} maxLength={64} required/>
      <Problem message={action.error}/>
      {!!result.length&&<ul className="max-h-40 overflow-auto text-sm">{result.map((text,i)=><li key={i}>{text}</li>)}</ul>}
    </div>
  </Dialog>
}

function BulkBets({users,selected,setSelected,back}) {
  const matches=useData(selected?root+'/matches/'+url(selected):null)
  const progress=useData(bulkPath,2500)
  const [picks,setPicks]=useState({})
  const [limit,setLimit]=useState(64)
  const [format,setFormat]=useState('express')
  const [lengths,setLengths]=useState([3])
  const [tiered,setTiered]=useState(false)
  const [shorter,setShorter]=useState(false)
  const [targetBalance,setTargetBalance]=useState(1500000)
  const single=format==='single'
  const [plan,setPlan]=useState(null)
  const [quote,setQuote]=useState(null)
  const action=useAction()
  const confirm=useConfirm()
  const opened=(matches.data?.items||[]).filter(match=>match.status==='betting_open')
  const selectedMatches=opened.filter(match=>picks[match.id]).map(match=>match.id)
  const job=progress.data?.job
  const todo=job?.items?.some(item=>resumable.has(item.state))
  const coverage=progress.data?.coverage
  const validMatches=single?selectedMatches.length===1:lengths.length>0&&selectedMatches.length>=Math.min(...lengths)&&selectedMatches.length<=12
  function chooseMatch(id, checked) {
    setPlan(null)
    setPicks(previous=>single?(checked?{[id]:true}:{}):({...previous,[id]:checked}))
  }
  function request(verb,data) {
    action.run(async()=>{
      if(verb==='start'&&!await confirm({title:'Начать массовые ставки?',text:`${plan.account_count} аккаунтов · ${num(plan.total_stake)} игровых монет`}))return
      if(verb==='resume'&&!await confirm({title:'Продолжить неотправленные ставки?',text:'Известные и неподтверждённые отправки не повторятся.'}))return
      if(verb==='stop'&&!await confirm({title:'Остановить отправку новых ставок?'}))return
      const result=await post(bulkPath+'/'+verb,data)
      if(verb==='plan'){setPlan(result);return}
      if(verb==='requote'){setQuote(result);return}
      setPlan(null);setQuote(null);await progress.reload()
    })
  }
  const planCols=[
    {accessorKey:'account',header:'Аккаунт'},{accessorKey:'variant',header:'Вариант'},
    {id:'coefficient',header:'Коэффициент',accessorFn:row=>Number(row.coefficient),cell:({getValue})=>'×'+num(getValue())},
    {id:'stake',header:'Ставка',accessorFn:row=>Number(row.stake),cell:({row,getValue})=><span title={row.original.tranches?.map(num).join(' + ')}>{num(getValue())}</span>},
    {id:'potential',header:'Выплата',accessorFn:row=>Number(row.potential),cell:({getValue})=>num(getValue())},
    {id:'outcomes',header:'Исходы',accessorFn:row=>row.legs.map(leg=>leg.side==='1'?leg.team_a:leg.team_b).join(', ')},
  ]
  const jobCols=[
    ...planCols.slice(0,4),
    planCols.at(-1),
    {id:'accepted_stake',header:'Принято монет',accessorFn:row=>Number(row.accepted_stake??(row.state==='ok'?row.stake:0)),cell:({getValue})=>num(getValue())},
    {id:'status',header:'Статус',accessorFn:item=>gameStatuses[item.state]||item.state,cell:({row,getValue})=><Badge variant="soft" color={row.original.state==='ok'?'success':row.original.state==='unknown'?'warning':'neutral'}>{getValue()}</Badge>},
    {accessorKey:'note',header:'Результат'},
    {id:'manual',header:'Проверка',cell:({row})=>!job.running&&row.original.state==='unknown'&&<Button size="sm" variant="outline" disabled={action.busy} onClick={()=>action.run(async()=>{
      if(!await confirm({title:'Подтвердить по истории игры?',text:`${row.original.account} · ${num(row.original.pending_bet?.stake??row.original.stake)} монет · вариант ${row.original.variant}`}))return
      await post(bulkPath+'/confirm-manual',{job_id:job.id,account:row.original.account,confirmed:true})
      await progress.reload()
    })}>Подтвердить вручную</Button>},
  ]
  return <>
    <PageHeader title="Вилка CS3BET" description="Предпросмотр покрытия исходов и распределения игровых монет.">
      <Button variant="outline" onClick={back}><ArrowLeft/>CS3BET</Button>
    </PageHeader>
    <Problem message={action.error||matches.error||progress.error}/>
    <Card title="Подбор матчей" description={single?"Первый по балансу ставит на исход 1, второй на исход 2, далее по очереди.":"Выберите до 12 открытых матчей. Коэффициент каждого купона ограничен ×30."}>
      <div className="grid gap-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <AccountPicker items={users} value={selected||''} onChange={value=>{setSelected(value);setPicks({});setPlan(null)}} label="Читать матчи через"/>
          <Picker label="Режим" items={[[ 'express','Экспрессы'],['single','Один матч · два исхода']]} value={format} onValueChange={value=>{setFormat(value);setPicks({});setPlan(null)}}/>
          {!single&&<Input label="Максимум вариантов" type="number" min={2} max={256} value={limit} onChange={event=>{setLimit(event.target.value);setPlan(null)}}/>}
          <Input label="Целевой баланс аккаунта" type="number" min={1} value={targetBalance} onChange={event=>{setTargetBalance(event.target.value);setPlan(null)}}/>
        </div>
        {!single&&<fieldset className="flex flex-wrap items-center gap-3">
          <legend className="mb-2 text-sm font-medium">Длина экспресса</legend>
          {[2,3,4,6,8].map(length=><Check key={length} checked={lengths.includes(length)} onChange={checked=>{
            setLengths(previous=>checked?[...previous,length]:previous.filter(value=>value!==length))
            setPlan(null)
          }}>{length} исхода</Check>)}
        </fieldset>}
        <div>
          <Check checked={tiered} onChange={value=>{setTiered(value);setPlan(null)}}>До 100 000 на аккаунт в несколько ставок</Check>
          {tiered&&<p className="text-sm text-muted-foreground">На один купон последовательно до 50 000, 32 500 и 17 500 в пределах баланса и лимитов игры.</p>}
          {!single&&<Check checked={shorter} onChange={value=>{setShorter(value);setPlan(null)}}>Разрешить укоротить купон, если коэффициент вырос выше ×30</Check>}
          {!single&&shorter&&<p className="text-sm text-muted-foreground">Укороченный купон может выйти за выбранную длину. Итоговый баланс и покрытие пересчитываются по принятым ставкам.</p>}
        </div>
        {!matches.data&&selected&&!matches.error?<Loading/>:!opened.length&&<Empty>Открытых матчей нет.</Empty>}
        {!!opened.length&&<div className="grid gap-2 md:grid-cols-2">{opened.map(match=><label key={match.id} className="flex items-center gap-2 rounded-lg border border-divider p-3 text-sm">
          <input type="checkbox" className="size-4 accent-primary" checked={!!picks[match.id]} onChange={event=>chooseMatch(match.id,event.target.checked)}/>
          <span className="flex-1">{match.team_a} — {match.team_b}</span><span className="text-muted-foreground">{num(match.odds_a)} / {num(match.odds_b)}</span>
        </label>)}</div>}
        <div><Button disabled={action.busy||job?.running||!validMatches||!(Number(targetBalance)>0)||(!single&&(Number(limit)<2||Number(limit)>256))}
          onClick={()=>request('plan',{match_ids:selectedMatches,max_variants:single?2:Number(limit),single,
            lengths:single?null:lengths,tiered,allow_shorter:!single&&shorter,target_balance:Number(targetBalance)})}><GitBranch/>Рассчитать распределение</Button></div>
      </div>
    </Card>
    {plan&&<Card title="Предпросмотр вариантов" description="Покрытие считается для выбранных бинарных исходов. Общий банк может уменьшиться."
      action={<Button disabled={action.busy||job?.running} onClick={()=>request('start',{plan_id:plan.id})}><CircleDollarSign/>Подтвердить отправку</Button>}>
      <p className="mb-3 text-lg font-semibold">
        Баланс лидера от {num(plan.worst_largest_balance)} до {num(plan.best_largest_balance)}
      </p>
      <div className="mb-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard label="Покрытие исходов" value={plan.complete_coverage?100:null} formatValue={value=>value+'%'}/>
        <MetricCard label="Мин. аккаунтов с целевым балансом" value={plan.minimum_accounts_at_target??null}/>
        <MetricCard label="Макс. аккаунтов с целевым балансом" value={plan.maximum_accounts_at_target??null}/>
        <MetricCard label="Мин. общий банк" value={Number(plan.worst_total_balance)}/>
      </div>
      <div className="mb-3 grid gap-1 text-sm text-muted-foreground">
        <p>Цель {num(plan.target_balance)} · исходов {plan.outcome_count} · купонов {plan.variant_count} · аккаунтов {plan.account_count}</p>
        <p>Наибольший выигравший коэффициент по исходам от ×{num(plan.worst_highest_coefficient)} до ×{num(plan.highest_planned_coefficient)} · ровно ×30 в плане {plan.exact_target_variants}</p>
        <p>Расчёт предполагает принятие ставок по показанным коэффициентам и победителя в каждом выбранном матче.</p>
      </div>
      <SortableTable data={plan.items} columns={planCols} getRowId={item=>item.account}/>
    </Card>}
    {job&&<Card title="Очередь ставок" description={job.running?'Выполняется':job.interrupted?'Требуется проверка очереди':'Обработка завершена'}>
      <div className="mb-3"><FormActions>
        {job.running&&<Button variant="outline" color="destructive" disabled={action.busy} onClick={()=>request('stop')}>Остановить новые отправки</Button>}
        {!job.running&&todo&&<>
          <Button variant="outline" disabled={action.busy} onClick={()=>request('resume',{job_id:job.id})}>Продолжить неотправленные</Button>
          <Button variant="outline" disabled={action.busy} onClick={()=>request('requote',{job_id:job.id})}>Пересчитать коэффициенты</Button>
        </>}
      </FormActions></div>
      {coverage&&<div className="mb-3 grid gap-1 text-sm">
        <p>По принятым ставкам покрыто {coverage.covered} из {coverage.total} исходов.</p>
        {coverage.covered===coverage.total&&<p>Баланс лидера от {num(coverage.worst_winner_balance)} по показанным сценариям.</p>}
        <p>Минимум {coverage.minimum_accounts_at_target} аккаунтов достигнут {num(coverage.target_balance)} по расчёту.</p>
      </div>}
      {quote?.job_id===job.id&&<div className="mb-3 grid gap-2 rounded-lg border border-divider p-3 text-sm">
        {quote.changes.map(change=><div key={change.index}>{change.account} · {change.error||`×${num(change.old_coefficient)} → ×${num(change.coefficient)}`}</div>)}
        <div><Button disabled={action.busy||quote.changes.some(change=>change.error)} onClick={()=>request('resume',{job_id:job.id,quote_id:quote.id})}>Принять пересчёт и продолжить</Button></div>
      </div>}
      <SortableTable data={job.items} columns={jobCols} getRowId={item=>item.account} autoResetPage={false}/>
    </Card>}
  </>
}
