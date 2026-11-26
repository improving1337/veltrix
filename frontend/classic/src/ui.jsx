import {createContext, useContext, useEffect, useRef, useState} from 'react'
import {AlertCircle, LoaderCircle} from 'lucide-react'
import {AlertDialog} from '@dashral/ui/components/alert-dialog'
import {Button} from '@dashral/ui/components/button'
import {Badge} from '@dashral/ui/components/badge'
import {Combobox} from '@dashral/ui/components/combobox'
import {Input} from '@dashral/ui/components/input'
import {Select} from '@dashral/ui/components/select'
import {Avatar} from '@dashral/ui/components/avatar'
import {accountPath, fullName, url} from './api'

const Confirmation = createContext(null)

export function ConfirmProvider({children}) {
  const [confirmation, setConfirmation] = useState(null)
  const pending = useRef(null)
  useEffect(() => () => pending.current?.(false), [])
  function confirm(options) {
    if (pending.current) return Promise.resolve(false)
    return new Promise(resolve => {pending.current = resolve;setConfirmation(options)})
  }
  function settle(value) {
    pending.current?.(value)
    pending.current = null
    setConfirmation(null)
  }
  return <Confirmation.Provider value={confirm}>
    {children}
    <AlertDialog open={!!confirmation} onOpenChange={open => {if (!open) settle(false)}}
      title={confirmation?.title || 'Подтвердите действие'} description={confirmation?.text}
      confirmLabel={confirmation?.label || 'Подтвердить'} cancelLabel="Отмена"
      color={confirmation?.danger ? 'destructive' : 'primary'} onConfirm={() => settle(true)}/>
  </Confirmation.Provider>
}
export const useConfirm = () => useContext(Confirmation)

export function Problem({message}) {
  return message && <p role="alert" className="flex items-start gap-2 text-sm text-destructive-text"><AlertCircle className="size-4 shrink-0"/>{message}</p>
}
export function Loading() {
  return <p role="status" className="flex gap-2 px-1 py-4 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin"/>Загрузка…</p>
}
export function Empty({children}) {
  return <p className="py-6 text-center text-sm text-muted-foreground">{children}</p>
}
export function TextArea({label, ...props}) {
  return <label className="grid min-w-0 gap-1.5 text-sm font-medium">{label}<textarea className="min-w-0 rounded-md border border-input bg-background px-3 py-2 text-sm font-normal outline-none focus-visible:ring-2 focus-visible:ring-ring/50" {...props}/></label>
}
export function Picker({items, ...props}) {
  return <Select options={items.map(item => Array.isArray(item) ? {value:String(item[0]),label:item[1]} : item)} {...props}/>
}
export function Check({children, checked, onChange, ...props}) {
  return <label className="flex items-center gap-2 py-1 text-sm"><input type="checkbox" className="size-4 accent-primary" checked={checked} onChange={event => onChange(event.target.checked)} {...props}/>{children}</label>
}
export function AccountPicker({items, value, onChange, label = 'Аккаунт'}) {
  const names = new Map()
  for (const account of items) {
    const name = fullName(account)
    names.set(name, (names.get(name) || 0) + 1)
  }
  return <Combobox label={label} placeholder="Выберите аккаунт" searchPlaceholder="Имя, username, номер или ID"
    emptyText="Аккаунты не найдены" value={value} onValueChange={onChange}
    options={items.map(account => {
      const name = fullName(account)
      return {value:account.name, label:names.get(name)>1 ? name+' · '+account.name : name,
        description:[account.username&&'@'+account.username,account.phone,account.id,account.name].filter(Boolean).join(' · ')}
    })}/>
}
export function AccountAvatar({account, size = 'sm', ...props}) {
  return <Avatar src={account.connected ? accountPath(account.name)+'/avatar?v='+url(account.avatar_ver || 0) : undefined} alt={fullName(account)} fallback={fullName(account).slice(0,2).toUpperCase()} size={size} {...props}/>
}
export function AccountState({account}) {
  const values = account.frozen ? ['destructive','Заморожен'] : account.connected ? ['success','Активен'] : ['neutral',account.status || 'Отключён']
  return <Badge variant="soft" color={values[0]}>{values[1]}</Badge>
}
export function FormActions({children}) {
  return <div className="flex flex-wrap items-center gap-2">{children}</div>
}
export function DownloadButton({filename, rows}) {
  function download() {
    const escape = value => {
      const text = String(value ?? '')
      return '"' + (/^[=+@\t\n\r-]/.test(text) ? "'" : '') + text.replaceAll('"','""') + '"'
    }
    const blob = new Blob(['\uFEFF',rows.map(row=>row.map(escape).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'})
    const href = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = href
    link.download = filename
    link.click()
    setTimeout(()=>URL.revokeObjectURL(href),1000)
  }
  return <Button variant="outline" onClick={download}>Экспорт CSV</Button>
}
