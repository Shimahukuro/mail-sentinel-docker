local common = dofile('/etc/mail-sentinel/common.lua')

options.timeout = common.positive_integer_env('IMAP_TIMEOUT_SECONDS')
options.info = false
options.hostnames = true
options.certificates = true

local inbox_name = common.required_env('IMAP_INBOX')
local junk_name = common.required_env('IMAP_JUNK')
local create_missing_folders = common.boolean_env('CREATE_MISSING_FOLDERS')
local learning_enabled = common.boolean_env('LEARNING_ENABLED')
local learn_ham_name = common.required_env('IMAP_LEARN_HAM')
local learn_spam_name = common.required_env('IMAP_LEARN_SPAM')
local learned_flag = common.required_env('LEARNED_FLAG')
local processed_flag = common.required_env('PROCESSED_FLAG')
local account = IMAP(common.account_options())

local function contains(values, expected)
    for _, value in ipairs(values) do if value == expected then return true end end
    return false
end

local function pass(check, fields)
    fields = fields or {}; fields.check = check; fields.result = 'pass'
    common.log_event('info', 'startup_diagnostic', fields)
end

local function run_diagnostics()
    if learning_enabled then
        assert(learn_ham_name ~= learn_spam_name, 'ham and spam learning folders must be different')
        assert(learn_ham_name ~= inbox_name and learn_ham_name ~= junk_name,
            'ham learning folder must differ from INBOX and Junk')
        assert(learn_spam_name ~= inbox_name and learn_spam_name ~= junk_name,
            'spam learning folder must differ from INBOX and Junk')
        assert(learned_flag ~= processed_flag, 'LEARNED_FLAG must differ from PROCESSED_FLAG')
    end
    assert(account:login(), 'IMAP authentication failed')
    pass('imap_connection')
    local mailboxes = account:list_all('', '*')
    assert(contains(mailboxes, inbox_name), 'configured INBOX folder does not exist')
    pass('inbox_folder', { folder = inbox_name })
    if contains(mailboxes, junk_name) then
        pass('junk_folder', { folder = junk_name })
    elseif create_missing_folders then
        common.log_event('warn', 'startup_diagnostic', {
            check = 'junk_folder', folder = junk_name, result = 'fallback', reason = 'will_create_during_monitoring'
        })
    else
        error('configured Junk folder does not exist')
    end
    local message_count = account[inbox_name]:check_status()
    assert(message_count ~= nil and message_count >= 0, 'INBOX status request failed')
    pass('inbox_read', { message_count = message_count })
    if learning_enabled then
        assert(contains(mailboxes, learn_ham_name), 'configured ham learning folder does not exist')
        assert(contains(mailboxes, learn_spam_name), 'configured spam learning folder does not exist')
        local ham_count = account[learn_ham_name]:check_status()
        local spam_count = account[learn_spam_name]:check_status()
        assert(ham_count ~= nil and ham_count >= 0, 'ham learning folder status request failed')
        assert(spam_count ~= nil and spam_count >= 0, 'spam learning folder status request failed')
        pass('learning_folders', {
            ham_folder = learn_ham_name, ham_message_count = ham_count,
            spam_folder = learn_spam_name, spam_message_count = spam_count
        })
    end
    local test_message = 'From: diagnostic@example.invalid\r\nTo: diagnostic@example.invalid\r\n' ..
        'Subject: Mail Sentinel diagnostic\r\n\r\nLocal connectivity test.\r\n'
    local status = pipe_to('spamc -x -c -d spamassassin -p 783 -s 65536 >/dev/null', test_message)
    assert(status == 0 or status == 1, 'SpamAssassin connectivity test failed')
    pass('spamassassin_connection')
    assert(account:logout(), 'IMAP logout failed')
end

local ok, diagnostic_error = pcall(run_diagnostics)
if not ok then
    common.log_event('error', 'startup_diagnostic', { result = 'fail', error = common.safe_error(diagnostic_error) })
    error('startup diagnostics failed')
end
common.log_event('info', 'startup_diagnostic_complete', { result = 'pass' })
