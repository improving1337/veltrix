// The layout and navigation styles are adapted from apps/demo's Classic template.
import {useEffect,useState} from 'react'
import {Bell, Bot, ChevronRight, Code2, Download, LayoutDashboard, LogOut, Menu, MessageSquare, Moon, PanelLeftClose, PanelLeftOpen, Search, Settings2, Shield, Sun, UserRound, Wrench, ChartNoAxesCombined, X} from 'lucide-react'
import {Avatar} from '@dashral/ui/components/avatar'
import {Badge} from '@dashral/ui/components/badge'
import {Button} from '@dashral/ui/components/button'
import {Dialog} from '@dashral/ui/components/dialog'
import {Input} from '@dashral/ui/components/input'
import {ScrollShadow} from '@dashral/ui/components/scroll-shadow'
import {indicatorMotion,useIndicator} from '@dashral/ui/lib/use-indicator'
import {cn} from '@dashral/ui/lib/utils'
import {post} from './api'
import {Problem,useConfirm} from './ui'

export const sections = [
  ['Панель', [
    ['overview','Overview',LayoutDashboard],
    ['accounts','Профили',UserRound],
    ['upload','Deploy',Download],
    ['messages','Inbox',MessageSquare],
  ]],
  ['Операции', [
    ['orders','Услуги',Wrench],
    ['automatic','Автопилот',Bot],
    ['cs3bet','CS3BET',ChartNoAxesCombined],
    ['scenarios','Сценарии',Code2],
    ['vpn','VPN и Mini App',Shield],
  ]],
  ['Администрирование', [['settings','Настройки',Settings2]]],
]
export const labels = Object.fromEntries(sections.flatMap(([,items])=>items.map(([id,label])=>[id,label])))

export function Logo({className}) {
  return <span aria-hidden="true" className={cn('grid size-7 shrink-0 place-items-center rounded-md bg-foreground text-[17px] font-black leading-none text-background',className)}>V</span>
}

function Sidebar({page, go, unread, hiddenTabs, collapsed, toggle, mobile, close}) {
  const [navRef, highlight] = useIndicator('[data-nav-current]')
  return <div className="flex h-full flex-col">
    <div className="flex h-16 shrink-0 items-center gap-2.5 pe-3 ps-5 collapsed:justify-center collapsed:px-0">
      <button onClick={() => go('overview')} className="flex min-w-0 cursor-pointer items-center gap-2.5 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/50 collapsed:hidden">
        <Logo/><span className="text-lg font-semibold tracking-tight">Veltrix</span>
      </button>
      {mobile ? <Button className="ms-auto" size="icon-sm" variant="ghost" onClick={close} aria-label="Закрыть меню"><X/></Button> :
        <Button variant="ghost" size="icon-sm" onClick={toggle} aria-label={collapsed?'Развернуть меню':'Свернуть меню'} className="ms-auto collapsed:mx-auto">{collapsed?<PanelLeftOpen/>:<PanelLeftClose/>}</Button>}
    </div>
    <ScrollShadow as="nav" ref={navRef} size={32} scrollbar data-measured={highlight?'':undefined} aria-label="Разделы панели"
      className="thin-scrollbar relative flex-1 space-y-6 overflow-x-hidden px-3 py-4 collapsed:space-y-3 collapsed:px-2">
      {highlight && <span aria-hidden className={cn(indicatorMotion,'rounded-md bg-sidebar-accent')} style={{translate:highlight.x+'px '+highlight.y+'px',width:highlight.width,height:highlight.height}}/>}
      {sections.map(([label,items]) => {
        const visible = items.filter(([id])=>!hiddenTabs.includes(id))
        if (!visible.length) return null
        return <div key={label}>
          <p className="mb-2 px-3 text-xs font-medium tracking-wide text-muted-foreground uppercase collapsed:sr-only">{label}</p>
          <ul className="space-y-0.5 collapsed:space-y-1">
            {visible.map(([id,title,Icon]) => <li key={id}><button type="button" title={title}
              aria-current={page===id?'page':undefined} data-nav-current={page===id||undefined} onClick={()=>go(id)}
              className={cn('relative flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-start text-[13px] font-medium transition-colors collapsed:mx-auto collapsed:size-10 collapsed:justify-center collapsed:px-0',page===id?'text-sidebar-accent-foreground not-in-data-measured:bg-sidebar-accent':'text-sidebar-foreground hover:bg-sidebar-accent/60 hover:text-sidebar-accent-foreground')}>
              <Icon className="size-4 shrink-0"/><span className="flex-1 collapsed:sr-only">{title}</span>
              {id==='messages'&&unread>0&&<Badge size="sm" className="collapsed:hidden">{unread}</Badge>}
            </button></li>)}
          </ul>
        </div>
      })}
    </ScrollShadow>
    <div className="border-t border-divider p-3 collapsed:px-2">
      <Button onClick={()=>go('scenarios',{wiki:true})} variant="ghost" className="w-full justify-start collapsed:justify-center" aria-label="Вики сценариев">
        <Code2/><span className="collapsed:sr-only">Вики</span>
      </Button>
    </div>
  </div>
}

export function Shell({page,navigate,unread,settings,children}) {
  const [collapsed,setCollapsed] = useState(()=>localStorage.getItem('veltrix.sidebar')==='collapsed')
  const [dark,setDark] = useState(()=>localStorage.getItem('veltrix.color')!=='light')
  const [mobile,setMobile] = useState(false)
  const [search,setSearch] = useState(false)
  const [query,setQuery] = useState('')
  const [error,setError] = useState('')
  const confirm=useConfirm()
  const hiddenTabs=(settings?.hidden_tabs||[]).filter(id=>id!=='overview'&&id!=='settings')
  const matches=Object.entries(labels).filter(([id,label])=>!hiddenTabs.includes(id)&&label.toLowerCase().includes(query.trim().toLowerCase()))
  function go(id, parameters) {
    setMobile(false);setSearch(false);setQuery('');navigate(id,parameters)
  }
  useEffect(()=> {
    document.documentElement.dataset.sidebar=collapsed?'collapsed':'open'
    localStorage.setItem('veltrix.sidebar',collapsed?'collapsed':'open')
  },[collapsed])
  useEffect(()=>{
    document.documentElement.classList.toggle('dark',dark)
    localStorage.setItem('veltrix.color',dark?'dark':'light')
  },[dark])
  useEffect(()=>{
    function keyboard(event) {
      if (!(event.ctrlKey||event.metaKey)||event.altKey) return
      if (event.key.toLowerCase()==='b'){event.preventDefault();setCollapsed(value=>!value)}
      if (event.key.toLowerCase()==='k'){event.preventDefault();setSearch(value=>!value)}
    }
    window.addEventListener('keydown',keyboard)
    return ()=>window.removeEventListener('keydown',keyboard)
  },[])
  async function logout() {
    if (!await confirm({title:'Выйти из панели?'})) return
    try {await post('/api/logout');location.replace('/login')} catch (error) {setError(error.message)}
  }
  const sidebar={page,go,unread,hiddenTabs,collapsed,toggle:()=>setCollapsed(value=>!value)}
  return <div className="flex min-h-svh">
    <aside data-sidebar-panel className="sticky top-0 z-40 hidden h-svh w-64 shrink-0 border-e border-divider bg-sidebar transition-[width] duration-(--duration-standard) ease-standard motion-reduce:transition-none lg:block collapsed:w-16">
      <Sidebar {...sidebar}/>
    </aside>
    <Dialog open={mobile} onOpenChange={setMobile} title="Разделы панели" showCloseButton={false}
      className="!left-0 !top-0 !m-0 !h-dvh !max-h-dvh !w-64 !max-w-full !translate-x-0 !translate-y-0 !rounded-none !bg-sidebar"
      classNames={{header:'sr-only',body:'!h-full !p-0'}}>
      <Sidebar {...sidebar} mobile close={()=>setMobile(false)}/>
    </Dialog>
    <div className="flex min-w-0 flex-1 flex-col">
      <header data-slot="site-header" className="sticky top-0 z-30 flex h-16 shrink-0 items-center gap-2 border-b border-divider bg-background/80 px-4 backdrop-blur md:px-6">
        <Button variant="ghost" size="icon-sm" className="lg:hidden" onClick={()=>setMobile(true)} aria-label="Открыть меню"><Menu/></Button>
        <div className="flex max-w-sm sm:flex-1">
          <Button variant="outline" className="justify-start text-muted-foreground sm:w-full" onClick={()=>setSearch(true)}><Search/><span>Поиск</span><kbd className="ms-auto hidden rounded border border-divider px-1.5 text-xs sm:inline">Ctrl K</kbd></Button>
        </div>
        <div className="ms-auto flex items-center gap-1">
          <Button variant="ghost" size="icon-sm" onClick={()=>setDark(value=>!value)} aria-label={dark?'Светлая тема':'Тёмная тема'}>{dark?<Sun/>:<Moon/>}</Button>
          <Button variant="ghost" size="icon-sm" onClick={()=>go('messages')} aria-label="Входящие" className="relative"><Bell/>{unread>0&&<i className="absolute end-0 top-0 size-2 rounded-full bg-primary"/>}</Button>
          <Button variant="ghost" onClick={logout} aria-label="Выйти" className="ms-2"><Avatar name="Veltrix Admin" size="sm"/><LogOut className="hidden sm:block"/></Button>
        </div>
      </header>
      <main className="w-full flex-1 space-y-6 p-4 md:p-6">
        <Problem message={error}/>{children}
      </main>
    </div>
    <Dialog open={search} onOpenChange={setSearch} title="Найти раздел">
      <Input value={query} onChange={event=>setQuery(event.target.value)} prefix={<Search/>} aria-label="Поиск по панели" onKeyDown={event=>{if(event.key==='Enter'&&matches[0])go(matches[0][0])}}/>
      <div className="mt-3 grid gap-1">
        {matches.map(([id,label])=><Button key={id} variant="ghost" className="justify-between" onClick={()=>go(id)}>{label}<ChevronRight/></Button>)}
        {!matches.length&&<span className="text-sm text-muted-foreground">Разделы не найдены</span>}
      </div>
    </Dialog>
  </div>
}
