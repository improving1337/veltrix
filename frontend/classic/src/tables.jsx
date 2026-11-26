import {useMemo} from 'react'
import {DataTable,DataTableColumnHeader} from '@dashral/ui/components/data-table'

export default function SortableTable({columns,...props}) {
  const sortable = useMemo(()=>columns.map(definition=>{
    const label = definition.header
    if (typeof label!=='string'||!label||(!definition.accessorKey&&!definition.accessorFn)||definition.enableSorting===false) {
      return definition
    }
    // Dashral takes column menu labels from the ID when the header is a component.
    return {...definition,id:label,meta:{...definition.meta,key:definition.id||definition.accessorKey},
      header:({column})=><DataTableColumnHeader column={column} title={label}/>}

  }),[columns])
  return <DataTable {...props} columns={sortable}/>
}
