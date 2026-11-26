// Layout sourced from the Classic Overview in apps/demo/app/(dashboard)/page.tsx.
import {lazy,Suspense,useState} from 'react'
import {CircleCheck, ShieldAlert, UserRoundCheck, Users} from 'lucide-react'
import {AreaChart} from '@dashral/ui/components/area-chart'
import {BarChart} from '@dashral/ui/components/bar-chart'
import {Button} from '@dashral/ui/components/button'
import {Card} from '@dashral/ui/components/card'
import {LineChart} from '@dashral/ui/components/line-chart'
import {MetricCard} from '@dashral/ui/components/metric-card'
import {Progress} from '@dashral/ui/components/progress'
import {Sparkline} from '@dashral/ui/components/sparkline'
import {Badge} from '@dashral/ui/components/badge'
import {moscow,num,useData} from './api'
import {Empty,Loading,Picker,Problem} from './ui'
import {PageHeader} from './page-header'

export const services = {channels:'Подписки',views:'Просмотры',reactions:'Реакции',reposts:'Репосты',polls:'Опросы',multi:'Уч',subentry:'Саб+Уч',starts:'Запуск бота',btn:'Нажатие кнопки',unsub:'Отписки'}
const SortableTable = lazy(()=>import('./tables'))
const postColumns = [
  {id:'post',header:'Публикация',accessorFn:post=>post.channel+' '+post.id,cell:({row})=>{
    const post = row.original
    return <>{post.channel.replace(/^https?:\/\/t\.me\//,'@')} · #{post.id}
      <Badge className="ms-2" variant="soft" color="neutral">{post.mode==='realistic'?'Реалистик':'Легаси'}</Badge></>
  }},
  {accessorKey:'done',header:'Выполнено',cell:({row})=>num(row.original.done)+' / '+num(row.original.target)},
  {accessorKey:'due',header:'По графику',cell:({getValue})=>num(getValue())},
  {id:'next',header:'Следующий шаг',accessorFn:post=>post.next ? Date.parse(post.next) : undefined,
    cell:({row})=>moscow(row.original.next),sortUndefined:'last'},
]
const accountSeries=[{key:'active',label:'Активные аккаунты',color:'var(--primary)'}]
const balanceSeries=[{key:'balance',label:'Общий баланс',color:'var(--primary)'},{key:'largest',label:'Наибольший баланс',color:'var(--success)'}]

export function History({data, type = 'active'}) {
  const [range,setRange]=useState('7')
  const cutoff=Date.now()/1000-Number(range)*86400
  const isBalance=type==='balance'
  const series=isBalance?balanceSeries:accountSeries
  const points=(data?.series||[]).filter(point=>point.at>=cutoff && series.some(item=>point[item.key]!=null))
  const step=Math.max(1,Math.ceil(points.length/100))
  const chart=points.filter((_,index)=>index%step===0||index===points.length-1)
    .map(point=>({...point,label:moscow(point.at,{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}))
  const Chart=isBalance?LineChart:AreaChart
  return <Card title={isBalance?'История баланса':'Активные аккаунты'} description="Данные Veltrix по московскому времени" className="min-w-0 xl:col-span-2"
    action={<Picker items={[[1,'24 часа'],[7,'7 дней'],[14,'14 дней']]} aria-label="Период" size="sm" value={range} onValueChange={setRange}/>}>
    {chart.length ? <Chart data={chart} index="label" series={series} height={288} locale="ru-RU" showLegend={isBalance} aria-label="История по времени"/> : <Empty>График появится после первого замера.</Empty>}
  </Card>
}

export function Overview({navigate}) {
  const {data,error}=useData('/api/overview',15000)
  const metrics=[
    ['accounts','Аккаунты',Users,'accounts','all'],
    ['active','Активные аккаунты',UserRoundCheck,'accounts','active'],
    ['frozen','Замороженные',ShieldAlert,'accounts','frozen'],
    ['completed','Действий выполнено',CircleCheck,'orders'],
  ]
  const totals=Object.entries(data?.services||{}).sort((a,b)=>b[1]-a[1])
  const barData=totals.map(([key,total])=>({label:services[key]||key,total}))
  const activeTrend=(data?.series||[]).slice(-40).map(point=>point.active)
  return <>
    <PageHeader title="Overview" description="Аккаунты, услуги и состояние Автопилота."/>
    <Problem message={error}/>
    <section className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4" aria-label="Ключевые показатели">
      {metrics.map(([key,label,Icon,page,filter])=><button type="button" key={key} onClick={()=>navigate(page,{filter})}
        className="min-w-0 cursor-pointer rounded-xl text-start outline-none transition-[box-shadow] hover:ring-1 hover:ring-border focus-visible:ring-2 focus-visible:ring-ring/50">
        <MetricCard className="h-full" label={label} icon={<Icon/>} value={data?.[key]??null} locale="ru-RU" countOnMount>
          {key==='active'&&activeTrend.length>1&&<Sparkline data={activeTrend} variant="area" className="h-9 text-primary" aria-label="Динамика активных аккаунтов"/>}
        </MetricCard>
      </button>)}
    </section>
    <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
      <History data={data}/>
      <Card title="Действия по услугам" description="Успешные действия аккаунтов">
        {barData.length?<BarChart data={barData} index="label" series={[{key:'total',label:'Действия',color:'var(--primary)'}]} height={288} locale="ru-RU" aria-label="Выполненные действия"/>:<Empty>Выполненных действий пока нет.</Empty>}
      </Card>
    </section>
    <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
      <Card title="Последние посты" description="Выполнение планов Автопилота" className="min-w-0 xl:col-span-2"
        action={<Button variant="outline" size="sm" onClick={()=>navigate('automatic')}>Автопилот</Button>}>
        {data?.posts?.length ?
          <Suspense fallback={<Loading/>}><SortableTable columns={postColumns} data={data.posts}
            getRowId={post=>post.channel+'/'+post.id} autoResetPage={false}/></Suspense>:
          <Empty>Нет постов с графиком просмотров.</Empty>}
      </Card>
      <Card title="Заказы в работе" action={<Button size="sm" variant="outline" onClick={()=>navigate('orders')}>Услуги</Button>}>
        {data?.running?.length ? <div className="grid gap-4">{data.running.map((order,index)=><div key={index} className="grid gap-1.5 text-sm">
          <div className="flex justify-between gap-2"><span>{services[order.service]||order.title}</span><span>{order.done}/{order.total}</span></div>
          <Progress value={100*order.done/(order.total||1)} aria-label="Выполнение"/>
        </div>)}</div>:<Empty>Активных заказов сейчас нет.</Empty>}
      </Card>
    </section>
  </>
}
