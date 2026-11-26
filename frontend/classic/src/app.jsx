import '@fontsource/inter/cyrillic-400.css'
import '@fontsource/inter/cyrillic-500.css'
import '@fontsource/inter/cyrillic-600.css'
import '@fontsource/inter/latin-400.css'
import '@fontsource/inter/latin-500.css'
import '@fontsource/inter/latin-600.css'
import './classic.css'

import {lazy,Suspense,useEffect,useState} from 'react'
import {createRoot} from 'react-dom/client'
import {LocaleProvider} from '@dashral/ui/lib/locale'
import {useData} from './api'
import {ConfirmProvider,Loading} from './ui'
import {labels,Shell} from './shell'
import {Login} from './login'
import {Overview} from './overview'

const Profiles=lazy(()=>import('./profiles').then(module=>({default:module.Profiles})))
const Orders=lazy(()=>import('./orders').then(module=>({default:module.Orders})))
const Inbox=lazy(()=>import('./inbox').then(module=>({default:module.Inbox})))
const Autopilot=lazy(()=>import('./autopilot').then(module=>({default:module.Autopilot})))
const Scenarios=lazy(()=>import('./scenarios').then(module=>({default:module.Scenarios})))
const Deploy=lazy(()=>import('./deploy').then(module=>({default:module.Deploy})))
const Settings=lazy(()=>import('./settings').then(module=>({default:module.Settings})))
const Tools=lazy(()=>import('./vpn').then(module=>({default:module.Tools})))
const Cs3bet=lazy(()=>import('./cs3bet').then(module=>({default:module.Cs3bet})))

const translations={
  close:'Закрыть',cancel:'Отмена',continue:'Продолжить',done:'Готово',remove:'Удалить',selected:'Выбрано',
  loading:'Загрузка…',noResults:'Ничего не найдено',noMatches:'Нет совпадений',noData:'Нет данных',
  search:'Поиск',searchTable:'Поиск по таблице',filters:'Фильтры',columns:'Столбцы',export:'Экспорт',
  selectAllRows:'Выбрать все строки',selectRow:'Выбрать строку',rowsPerPage:'Строк на странице',
  previousPage:'Предыдущая страница',nextPage:'Следующая страница',firstPage:'Первая страница',lastPage:'Последняя страница',
  pageOf:'Страница {page} из {total}',
}

function route() {
  const [section,query]=location.hash.slice(1).split('?')
  return {page:Object.hasOwn(labels,section)?section:'overview',parameters:Object.fromEntries(new URLSearchParams(query))}
}
function App() {
  const [{page,parameters},setRoute]=useState(route)
  const preferences=useData('/api/panel-settings')
  const accounts=useData('/api/accounts',30000)
  const inbox=useData('/api/inbox',45000)
  const reloadInbox=inbox.reload
  const settings=preferences.data
  useEffect(()=>{
    const listener=()=>setRoute(route())
    window.addEventListener('hashchange',listener)
    window.addEventListener('popstate',listener)
    return ()=>{window.removeEventListener('hashchange',listener);window.removeEventListener('popstate',listener)}
  },[])
  useEffect(()=>{
    const stream=new EventSource('/api/events')
    let timer
    stream.onmessage=event=>{
      let data
      try{data=JSON.parse(event.data)}catch{return}
      if(!data||typeof data!=='object')return
      window.dispatchEvent(new CustomEvent('veltrix:event',{detail:data}))
      if(data.card||data.type==='read'){clearTimeout(timer);timer=setTimeout(reloadInbox,350)}
    }
    stream.onopen=reloadInbox
    return ()=>{clearTimeout(timer);stream.close()}
  },[reloadInbox])
  function navigate(id,params={}) {
    if(!Object.hasOwn(labels,id))return
    const query=new URLSearchParams(Object.entries(params).filter(([,value])=>value!=null)).toString()
    const hash=id==='overview'?'':'#'+id+(query?'?'+query:'')
    if(location.hash!==hash)history.pushState(null,'',location.pathname+hash)
    setRoute({page:id,parameters:params})
    window.scrollTo({top:0,behavior:'instant'})
  }
  useEffect(()=>{document.title=labels[page]+' — Veltrix'},[page])
  const items=accounts.data?.items||[]
  const screens={
    overview:<Overview navigate={navigate}/>,
    accounts:<Profiles key={JSON.stringify(parameters)} navigate={navigate} parameters={parameters} settings={settings} query={accounts}/>,
    upload:<Deploy settings={settings} reloadSettings={preferences.reload} reloadAccounts={accounts.reload}/>,
    messages:<Inbox accounts={items} inbox={inbox}/>,
    orders:<Orders/>,
    automatic:<Autopilot/>,
    cs3bet:<Cs3bet accounts={items}/>,
    scenarios:<Scenarios key={JSON.stringify(parameters)} parameters={parameters}/>,
    vpn:<Tools accounts={items}/>,
    settings:<Settings settings={settings} reload={preferences.reload}/>,
  }
  return <Shell page={page} navigate={navigate} unread={inbox.data?.unread||0} settings={settings}><Suspense fallback={<Loading/>}>{screens[page]}</Suspense></Shell>
}

createRoot(document.getElementById('root')).render(
  <LocaleProvider locale="ru-RU" messages={translations}>
    <ConfirmProvider>{location.pathname==='/login'?<Login/>:<App/>}</ConfirmProvider>
  </LocaleProvider>,
)
