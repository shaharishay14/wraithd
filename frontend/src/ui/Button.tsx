import type { AnchorHTMLAttributes, ButtonHTMLAttributes } from 'react'

import { cn } from '../lib/cn'
import { buttonClass, type Size, type Variant } from './buttonClass'
import { Spinner } from './Spinner'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
}

export function Button({ variant, size, loading = false, disabled, className, children, type = 'button', ...props }: ButtonProps) {
  return (
    <button type={type} className={buttonClass(variant, size, className)} disabled={disabled || loading} aria-busy={loading || undefined} {...props}>
      {loading && <Spinner size={14} tone={variant === 'white' || variant === 'danger' || !variant ? 'dark' : 'light'} />}
      {children}
    </button>
  )
}

export function LinkButton({ variant, size, className, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { variant?: Variant; size?: Size }) {
  return <a className={buttonClass(variant, size, className)} {...props} />
}

// Round icon-only button. Always pass an aria-label.
export function IconButton({ className, type = 'button', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { 'aria-label': string }) {
  return (
    <button
      type={type}
      className={cn(
        'inline-grid size-9 shrink-0 place-items-center rounded-pill text-white/75 transition-colors hover:bg-white/10 hover:text-white',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white disabled:pointer-events-none disabled:opacity-50',
        '[&_svg]:size-4 [&_svg]:stroke-[1.7]',
        className,
      )}
      {...props}
    />
  )
}
