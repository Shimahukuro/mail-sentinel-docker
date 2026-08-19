local common = dofile('/etc/mail-sentinel/common.lua')

options.timeout = common.positive_integer_env('IMAP_TIMEOUT_SECONDS')
options.info = false
options.create = true
options.hostnames = true
options.certificates = true

local account = IMAP(common.account_options())
assert(account:login(), 'IMAP authentication failed')

local existing = account:list_all('', '*')
local function ensure_folder(name)
    for _, current in ipairs(existing) do
        if current == name then return end
    end
    assert(account:create_mailbox(name), 'failed to create test mailbox: ' .. name)
end

ensure_folder('Junk')
ensure_folder('Learn-Ham')
ensure_folder('Learn-Spam')
assert(account:logout(), 'IMAP logout failed')
