options.charset = 'UTF-8'

local function required_env(name)
    local value = os.getenv(name)
    if value == nil or value == '' then error('required environment variable is missing: ' .. name) end
    return value
end

local function positive_integer_env(name)
    local raw = required_env(name)
    local value = tonumber(raw)
    if value == nil or value < 1 or value % 1 ~= 0 then error(name .. ' must be a positive integer') end
    return value
end

local function nonnegative_integer_env(name)
    local raw = required_env(name)
    local value = tonumber(raw)
    if value == nil or value < 0 or value % 1 ~= 0 then error(name .. ' must be a nonnegative integer') end
    return value
end

local function boolean_env(name)
    local value = required_env(name):lower()
    if value == 'true' then return true end
    if value == 'false' then return false end
    error(name .. ' must be true or false')
end

local function read_secret(path)
    local file, open_error = io.open(path, 'r')
    if file == nil then error('cannot read IMAP password secret: ' .. tostring(open_error)) end
    local value = file:read('*a')
    file:close()
    value = value:gsub('[\r\n]+$', '')
    if value == '' then error('IMAP password secret is empty') end
    return value
end

local function json_escape(value)
    return tostring(value):gsub('\\', '\\\\'):gsub('"', '\\"'):gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
end

local function json_value(value)
    if type(value) == 'number' then return tostring(value) end
    if type(value) == 'boolean' then return value and 'true' or 'false' end
    return '"' .. json_escape(value) .. '"'
end

local function log_event(level, event, fields)
    local values = {
        '"timestamp":"' .. os.date('!%Y-%m-%dT%H:%M:%SZ') .. '"',
        '"level":"' .. json_escape(level) .. '"',
        '"event":"' .. json_escape(event) .. '"'
    }
    local keys = {}
    for key, value in pairs(fields or {}) do if value ~= nil then table.insert(keys, key) end end
    table.sort(keys)
    for _, key in ipairs(keys) do
        table.insert(values, '"' .. json_escape(key) .. '":' .. json_value(fields[key]))
    end
    print('{' .. table.concat(values, ',') .. '}')
end

local imap_username = required_env('IMAP_USERNAME')
local imap_password = read_secret(required_env('IMAP_PASSWORD_FILE'))

local function redact_plain(value, secret, replacement)
    local parts = {}
    local start = 1
    while true do
        local first, last = value:find(secret, start, true)
        if first == nil then
            table.insert(parts, value:sub(start))
            break
        end
        table.insert(parts, value:sub(start, first - 1))
        table.insert(parts, replacement)
        start = last + 1
    end
    return table.concat(parts)
end

local function safe_error(value)
    local message = tostring(value):gsub('[\r\n]+', ' ')
    message = redact_plain(message, imap_username, '<account>')
    message = redact_plain(message, imap_password, '<secret>')
    return message
end

local function account_options()
    local tls_mode = required_env('IMAP_TLS_MODE')
    local settings = {
        server = required_env('IMAP_HOST'), port = positive_integer_env('IMAP_PORT'),
        username = imap_username, password = imap_password
    }
    if tls_mode == 'implicit' then
        settings.ssl = 'auto'
    elseif tls_mode == 'starttls' then
        options.starttls = true
    elseif tls_mode == 'none' then
        options.starttls = false
    else
        error('IMAP_TLS_MODE must be implicit, starttls, or none')
    end
    return settings
end

return {
    required_env = required_env, positive_integer_env = positive_integer_env,
    nonnegative_integer_env = nonnegative_integer_env, boolean_env = boolean_env,
    log_event = log_event, safe_error = safe_error, account_options = account_options
}
