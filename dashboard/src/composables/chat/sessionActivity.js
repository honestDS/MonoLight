export const formatSessionActivityTime = (date = new Date()) => {
  const value = date instanceof Date ? date : new Date(date)
  return [
    value.getFullYear(),
    String(value.getMonth() + 1).padStart(2, '0'),
    String(value.getDate()).padStart(2, '0')
  ].join('-') + ' ' + [
    String(value.getHours()).padStart(2, '0'),
    String(value.getMinutes()).padStart(2, '0'),
    String(value.getSeconds()).padStart(2, '0')
  ].join(':')
}

export const withSessionActivity = (session, now = new Date()) => ({
  ...session,
  last_active: session?.last_active || formatSessionActivityTime(now)
})
