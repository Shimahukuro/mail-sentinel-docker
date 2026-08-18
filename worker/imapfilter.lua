local common = dofile('/etc/mail-sentinel/common.lua')

local inbox_name = common.required_env('IMAP_INBOX')
local junk_name = common.required_env('IMAP_JUNK')
local processed_flag = common.required_env('PROCESSED_FLAG')
local create_missing_folders = common.boolean_env('CREATE_MISSING_FOLDERS')
local dry_run = common.boolean_env('DRY_RUN')
local poll_interval = common.positive_integer_env('POLL_INTERVAL_SECONDS')
local batch_size = common.positive_integer_env('BATCH_SIZE')
local lookback_days = common.nonnegative_integer_env('LOOKBACK_DAYS')
local spamc_max_size = common.positive_integer_env('SPAMC_MAX_SIZE_BYTES')
local retry_initial = common.positive_integer_env('RETRY_INITIAL_SECONDS')
local retry_max = common.positive_integer_env('RETRY_MAX_SECONDS')

if retry_initial > retry_max then error('RETRY_INITIAL_SECONDS must not exceed RETRY_MAX_SECONDS') end

options.timeout = common.positive_integer_env('IMAP_TIMEOUT_SECONDS')
options.info = false
options.limit = batch_size
options.range = batch_size
options.create = create_missing_folders
options.hostnames = true
options.certificates = true

local spamc_result_path = '/tmp/spamc-result'
local spamc_command = string.format(
    'spamc -x -c -d spamassassin -p 783 -s %d >%s', spamc_max_size, spamc_result_path
)

local function read_spamc_score()
    local file = io.open(spamc_result_path, 'r')
    if file == nil then return nil end
    local result = file:read('*a')
    file:close()
    os.remove(spamc_result_path)
    return result:match('^%s*([^%s]+)')
end

local function ensure_junk_folder(account)
    if not create_missing_folders then return end
    local mailboxes = account:list_all('', '*')
    for _, mailbox_name in ipairs(mailboxes) do if mailbox_name == junk_name then return end end
    assert(account:create_mailbox(junk_name), 'failed to create Junk mailbox')
    common.log_event('info', 'folder_created', { folder = junk_name })
end

local function process_message(message, junk)
    local mailbox, uid = table.unpack(message)
    local message_size = mailbox[uid]:fetch_size()
    if message_size > spamc_max_size then
        common.log_event('warn', 'message_deferred', {
            uid = uid, reason = 'message_too_large', size_bytes = message_size
        })
        return 'deferred'
    end

    local header = mailbox[uid]:fetch_header()
    local body = mailbox[uid]:fetch_body()
    local content = header:gsub('[\r\n]+$', '\r\n') .. '\r\n' .. body
    local status = pipe_to(spamc_command, content)
    local score = read_spamc_score()
    local selected = Set {}
    table.insert(selected, message)

    if status == 1 then
        if not dry_run then assert(selected:move_messages(junk), 'failed to move spam message') end
        common.log_event('info', 'message_classified', {
            uid = uid, classification = 'spam', score = score or 'unknown',
            action = dry_run and 'would_move' or 'moved', destination = junk_name, dry_run = dry_run
        })
        return 'spam'
    elseif status == 0 then
        if not dry_run then assert(selected:add_flags({ processed_flag }), 'failed to mark ham message') end
        common.log_event('info', 'message_classified', {
            uid = uid, classification = 'ham', score = score or 'unknown',
            action = dry_run and 'would_mark' or 'marked', dry_run = dry_run
        })
        return 'ham'
    end

    common.log_event('warn', 'message_deferred', {
        uid = uid, reason = 'spamassassin_error', spamc_exit = status
    })
    error('SpamAssassin classification failed')
end

local function scan_once()
    local account = IMAP(common.account_options())
    assert(account:login(), 'IMAP authentication failed')
    ensure_junk_folder(account)
    local inbox = account[inbox_name]
    local junk = account[junk_name]
    local candidates = inbox:has_unkeyword(processed_flag) * inbox:is_newer(lookback_days)
    local processed, spam, ham, deferred = 0, 0, 0, 0

    for _, message in ipairs(candidates) do
        if processed >= batch_size then break end
        local result = process_message(message, junk)
        if result == 'spam' then spam = spam + 1 end
        if result == 'ham' then ham = ham + 1 end
        if result == 'deferred' then deferred = deferred + 1 end
        processed = processed + 1
    end

    assert(account:logout(), 'IMAP logout failed')
    common.log_event('info', 'scan_complete', {
        processed = processed, spam = spam, ham = ham, deferred = deferred, dry_run = dry_run
    })
end

common.log_event('info', 'worker_started', {
    dry_run = dry_run, poll_interval_seconds = poll_interval, batch_size = batch_size
})

local retry_delay = retry_initial
while true do
    local ok, scan_error = pcall(scan_once)
    if ok then
        retry_delay = retry_initial
        sleep(poll_interval)
    else
        common.log_event('error', 'scan_failed', {
            error = common.safe_error(scan_error), retry_in_seconds = retry_delay
        })
        sleep(retry_delay)
        retry_delay = math.min(retry_delay * 2, retry_max)
    end
end
