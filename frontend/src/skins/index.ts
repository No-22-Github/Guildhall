export type StationId =
  | 'receptionist'
  | 'board'
  | 'adventurer'
  | 'appraiser'
  | 'archive'
  | 'settings'
export interface Skin {
  id: string
  layout?: 'workbench'
  name: string
  description: string
  room: string
  sprites: string
  palette: { accent: string; background: string }
  filter: string
  stations: {
    id: StationId
    label: string
    hint: string
    x: number
    y: number
    sprite?: number
    clipBottom?: number
  }[]
  board: { x: number; y: number; width: number; height: number }
}
const tavern: Skin = {
  id: 'tavern',
  name: '琥珀酒馆',
  description: '暖木、炉火与一张新的委托。',
  room: '/skins/tavern/room.png',
  sprites: '/skins/tavern/characters.png',
  palette: { accent: '#e9b968', background: '#181610' },
  filter: 'none',
  stations: [
    {
      id: 'receptionist',
      label: '前台',
      hint: '聊聊你的新想法',
      x: 21,
      y: 35,
      sprite: 0,
      clipBottom: 36,
    },
    { id: 'board', label: '委托板', hint: '查看、张贴与派单', x: 50, y: 42 },
    {
      id: 'adventurer',
      label: '冒险者',
      hint: '出发与执行记录',
      x: 29,
      y: 78,
      sprite: 1,
    },
    {
      id: 'appraiser',
      label: '鉴定台',
      hint: '验收证据与交付',
      x: 79,
      y: 36,
      sprite: 2,
      clipBottom: 36,
    },
    {
      id: 'archive',
      label: '档案柜',
      hint: '已完成与放弃的委托',
      x: 94,
      y: 78,
    },
    {
      id: 'settings',
      label: '公会手册',
      hint: '模型设置与大厅外观',
      x: 65,
      y: 72,
    },
  ],
  board: { x: 39, y: 16, width: 20, height: 20 },
}
export const skins: Skin[] = [
  {
    id: 'workbench', layout: 'workbench', name: '工作台',
    description: '简洁专注的工作区，支持浅色与深色。',
    room: '', sprites: '', filter: 'none',
    palette: { accent: '#343936', background: '#f0f1ed' },
    stations: [], board: { x: 0, y: 0, width: 0, height: 0 },
  },
  tavern,
  {
    ...tavern,
    id: 'moonlight',
    name: '月光酒馆',
    description: '同一间酒馆，偏冷的夜间色调。',
    filter: 'saturate(.72) hue-rotate(15deg) brightness(.8)',
    palette: { accent: '#bed1bf', background: '#11191a' },
  },
]
export function getSkin(id: string) {
  return skins.find((s) => s.id === id) ?? skins[0]
}
