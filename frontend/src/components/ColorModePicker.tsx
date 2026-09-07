import { Monitor, Moon, Sun } from 'lucide-react'
import { Button } from './ui/button'
import type { ColorMode } from '../useColorMode'

const options = [
  { value: 'light', label: '浅色', Icon: Sun },
  { value: 'dark', label: '深色', Icon: Moon },
  { value: 'system', label: '跟随系统', Icon: Monitor },
] as const

export default function ColorModePicker({
  value,
  onChange,
  compact = false,
}: {
  value: ColorMode
  onChange: (mode: ColorMode) => void
  compact?: boolean
}) {
  return (
    <div className="color-mode-picker" role="group" aria-label="显示模式">
      {options.map(({ value: mode, label, Icon }) => (
        <Button
          key={mode}
          variant="ghost"
          size={compact ? 'icon-sm' : 'sm'}
          aria-label={label}
          title={label}
          aria-pressed={value === mode}
          onClick={() => onChange(mode)}
        >
          <Icon aria-hidden="true" />
          {!compact && label}
        </Button>
      ))}
    </div>
  )
}
