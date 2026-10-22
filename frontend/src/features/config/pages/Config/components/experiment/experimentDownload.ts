export const downloadBlob = (blob: Blob, fileName: string) => {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

export const downloadFileName = (contentDisposition: string | undefined, fallback: string) => {
  if (!contentDisposition) return fallback
  const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  let candidate: string | undefined
  if (encoded) {
    try {
      candidate = decodeURIComponent(encoded)
    } catch {
      candidate = encoded
    }
  } else {
    candidate = contentDisposition.match(/filename="?([^";]+)"?/i)?.[1]
  }
  if (!candidate) return fallback
  const baseName = candidate.split(/[\\/]/).pop()
    ?.replace(/[<>:"|?*]/g, '')
    .split('')
    .filter((character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127)
    .join('')
    .trim()
  return baseName && baseName !== '.' && baseName !== '..' ? baseName : fallback
}
