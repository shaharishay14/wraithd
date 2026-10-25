import { cn } from '../lib/cn'

// Sky buttons are pills: white with dark text, or glass with a thin white outline. Never white on white.
export type Variant = 'white' | 'glass' | 'ghost' | 'danger'
export type Size = 'sm' | 'md'

const VARIANT: Record<Variant, string> = {
  white: 'bg-white text-on-white hover:bg-[#f1ede8]',
  glass: 'border-edge-strong bg-white/8 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.25)] hover:bg-white/16',
  ghost: 'bg-transparent text-white/80 hover:bg-white/8 hover:text-white',
  danger: 'bg-off text-on-white hover:bg-[#ff9d8f]',
}

const SIZE: Record<Size, string> = {
  sm: 'h-8 px-3.5 text-[13px]',
  md: 'h-10 px-5 text-sm',
}

export const buttonClass = (variant: Variant = 'white', size: Size = 'md', className?: string) =>
  cn(
    'inline-flex shrink-0 items-center justify-center gap-2 rounded-pill border border-transparent font-semibold whitespace-nowrap no-underline',
    'transition-[background-color,border-color,color,transform] duration-150 active:scale-[0.98]',
    'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white',
    'disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:stroke-[1.7]',
    VARIANT[variant],
    SIZE[size],
    className,
  )
