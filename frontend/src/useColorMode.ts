import { useEffect, useState } from 'react'

export type ColorMode = 'light' | 'dark' | 'system'
const key = 'guildhall.colorMode'
const valid = (value: string | null): ColorMode =>
  value === 'light' || value === 'dark' ? value : 'system'

export function useColorMode() {
  const [mode, setMode] = useState<ColorMode>(() => {
    try {
      return valid(localStorage.getItem(key))
    } catch {
      return 'system'
    }
  })
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia('(prefers-color-scheme: dark)').matches,
  )
  useEffect(() => {
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const update = () => setSystemDark(query.matches)
    const sync = (event: StorageEvent) => {
      if (event.key === key || event.key === null)
        setMode(valid(event.newValue))
    }
    update()
    query.addEventListener('change', update)
    window.addEventListener('storage', sync)
    return () => {
      query.removeEventListener('change', update)
      window.removeEventListener('storage', sync)
    }
  }, [])
  function changeMode(value: ColorMode) {
    setMode(value)
    try {
      localStorage.setItem(key, value)
    } catch {
      /* Optional browser preference. */
    }
  }
  return {
    mode,
    changeMode,
    resolved: mode === 'system' ? (systemDark ? 'dark' : 'light') : mode,
  }
}
