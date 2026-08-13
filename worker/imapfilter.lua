local function required_env(name)
    local value = os.getenv(name)
    if value == nil or value == '' then
        error('required environment variable is missing: ' .. name)
    end
    return value
end

local function positive_integer_env(name)
    local raw = required_env(name)
    local value = tonumber(raw)
    if value == nil or value < 1 or value % 1 ~= 0 then
        error(name .. ' must be a positive integer')
    end
    return value
end

local function nonnegative_integer_env(name)
    local raw = required_env(name)
    local value = tonumber(raw)
    if value == nil or value < 0 or value % 1 ~= 0 then
        error(name .. ' must be a nonnegative integer')
    end
    return value
end

local function boolean_env(name)
    local value = required_env(name):lower()
    if value == 'true' then
        return true
    end
    if value == 'false' then
        return false
    end
    error(name .. ' must be true or false')
end

local function read_secret(path)
    local file, open_error = io.open(path, 'r')
    if file == nil then
        error('cannot read IMAP password secret: ' .. tostring(open_error))
    end
    local value = file:read('*a')
    file:close()
    value = value:gsub('[\r\n]+$', '')
    if value == '' then
        error('IMAP password secret is empty')
    end
    return value
end

local imap_host = required_env('IMAP_HOST')
local imap_port = positive_integer_env('IMAP_PORT')
local imap_tls_mode = required_env('IMAP_TLS_MODE')
local imap_username = required_env('IMAP_USERNAME')
local imap_password = read_secret(required_env('IMAP_PASSWORD_FILE'))
local inbox_name = required_env('IMAP_INBOX')
local junk_name = required_env('IMAP_JUNK')
local processed_flag = required_env('PROCESSED_FLAG')
local create_missing_folders = boolean_env('CREATE_MISSING_FOLDERS')
local poll_interval = positive_integer_env('POLL_INTERVAL_SECONDS')
local batch_size = positive_integer_env('BATCH_SIZE')
local lookback_days = nonnegative_integer_env('LOOKBACK_DAYS')
local spamc_max_size = positive_integer_env('SPAMC_MAX_SIZE_BYTES')

options.timeout = positive_integer_env('IMAP_TIMEOUT_SECONDS')
options.info = true
options.limit = batch_size
options.range = batch_size
options.create = create_missing_folders
options.hostnames = true
options.certificates = true

local account_options = {
    server = imap_host,
    port = imap_port,
    username = imap_username,
    password = imap_password
}

if imap_tls_mode == 'implicit' then
    account_options.ssl = 'auto'
elseif imap_tls_mode == 'starttls' then
    options.starttls = true
elseif imap_tls_mode == 'none' then
    options.starttls = false
else
    error('IMAP_TLS_MODE must be implicit, starttls, or none')
end

local account = IMAP(account_options)

if create_missing_folders then
    local mailboxes = account:list_all()
    local junk_exists = false
    for _, mailbox_name in ipairs(mailboxes) do
        if mailbox_name == junk_name then
            junk_exists = true
            break
        end
    end
    if not junk_exists then
        assert(account:create_mailbox(junk_name), 'failed to create Junk mailbox')
    end
end

local inbox = account[inbox_name]
local junk = account[junk_name]
local spamc_command = string.format(
    'spamc -x -c -d spamassassin -p 783 -s %d',
    spamc_max_size
)

local function scan_once()
    local candidates = inbox:has_unkeyword(processed_flag) * inbox:is_newer(lookback_days)
    local processed = 0

    for _, message in ipairs(candidates) do
        if processed >= batch_size then
            break
        end

        local mailbox, uid = table.unpack(message)
        local message_size = mailbox[uid]:fetch_size()

        if message_size > spamc_max_size then
            print(string.format('scan deferred: uid=%s reason=message_too_large size=%s', tostring(uid), tostring(message_size)))
        else
            local header = mailbox[uid]:fetch_header()
            local body = mailbox[uid]:fetch_body()
            local content = header:gsub('[\r\n]+$', '\r\n') .. '\r\n' .. body
            local status = pipe_to(spamc_command, content)
            local selected = Set {}
            table.insert(selected, message)

            if status == 1 then
                selected:move_messages(junk)
                print(string.format('spam moved: uid=%s', tostring(uid)))
            elseif status == 0 then
                selected:add_flags({ processed_flag })
                print(string.format('ham checked: uid=%s', tostring(uid)))
            else
                print(string.format('scan deferred: uid=%s spamc_exit=%s', tostring(uid), tostring(status)))
            end
        end

        processed = processed + 1
    end
end

while true do
    scan_once()
    sleep(poll_interval)
end
