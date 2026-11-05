# Bootstrap Keycloak realm/client/roles for LinkedEye SSO
# Requires Keycloak running on http://127.0.0.1:8083
$ErrorActionPreference = "Stop"

$kcBase = if ($env:KEYCLOAK_SERVER_URL) { $env:KEYCLOAK_SERVER_URL.TrimEnd('/') } else { "http://127.0.0.1:8083" }
$realm = if ($env:KEYCLOAK_REALM) { $env:KEYCLOAK_REALM } else { "linkedeye" }
$clientId = if ($env:KEYCLOAK_CLIENT_ID) { $env:KEYCLOAK_CLIENT_ID } else { "linkedeye-web" }
$clientSecret = if ($env:KEYCLOAK_CLIENT_SECRET) { $env:KEYCLOAK_CLIENT_SECRET } else { "linkedeye-kc-dev-secret-2026" }
$adminUser = if ($env:KEYCLOAK_ADMIN_USER) { $env:KEYCLOAK_ADMIN_USER } else { "admin" }
$adminPass = if ($env:KEYCLOAK_ADMIN_PASSWORD) { $env:KEYCLOAK_ADMIN_PASSWORD } else { "admin123456" }
$portalUrl = if ($env:PORTAL_URL) { $env:PORTAL_URL.TrimEnd('/') } else { "http://127.0.0.1:8000" }

Write-Host "Keycloak setup -> $kcBase realm=$realm"

$token = (Invoke-RestMethod -Method Post -Uri "$kcBase/realms/master/protocol/openid-connect/token" `
    -ContentType "application/x-www-form-urlencoded" `
    -Body "grant_type=password&client_id=admin-cli&username=$adminUser&password=$adminPass").access_token
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

$realmBody = @{
    realm = $realm
    enabled = $true
    displayName = "LinkedEye"
    registrationAllowed = $false
    loginWithEmailAllowed = $true
    sslRequired = "none"
} | ConvertTo-Json
try { Invoke-RestMethod -Method Post -Uri "$kcBase/admin/realms" -Headers $headers -Body $realmBody | Out-Null; Write-Host "Realm created" }
catch { Write-Host "Realm exists or update skipped" }

$roles = @("Admin","ViewOnly","Management","Onboard","UserView","TechInfra","Risk","TradeSupport","Google","O365","DjangoAdmin")
foreach ($role in $roles) {
    try {
        Invoke-RestMethod -Method Post -Uri "$kcBase/admin/realms/$realm/roles" -Headers $headers -Body (@{ name = $role } | ConvertTo-Json) | Out-Null
        Write-Host "Role $role created"
    } catch { Write-Host "Role $role exists" }
}

$client = @{
    clientId = $clientId
    name = "LinkedEye Web Portal"
    enabled = $true
    publicClient = $false
    clientAuthenticatorType = "client-secret"
    secret = $clientSecret
    redirectUris = @("$portalUrl/auth/oidc/callback/", "$portalUrl/*")
    webOrigins = @($portalUrl, "+")
    standardFlowEnabled = $true
    directAccessGrantsEnabled = $true
    protocol = "openid-connect"
    fullScopeAllowed = $true
} | ConvertTo-Json -Depth 5

$existing = Invoke-RestMethod -Method Get -Uri "$kcBase/admin/realms/$realm/clients?clientId=$clientId" -Headers $headers
if ($existing.Count -eq 0) {
    Invoke-RestMethod -Method Post -Uri "$kcBase/admin/realms/$realm/clients" -Headers $headers -Body $client | Out-Null
    Write-Host "Client created"
} else {
    Invoke-RestMethod -Method Put -Uri "$kcBase/admin/realms/$realm/clients/$($existing[0].id)" -Headers $headers -Body $client | Out-Null
    Write-Host "Client updated"
}

$clientUuid = (Invoke-RestMethod -Method Get -Uri "$kcBase/admin/realms/$realm/clients?clientId=$clientId" -Headers $headers)[0].id

$claimMappers = @(
    @{
        name = "linkedeye-site-ids"
        protocol = "openid-connect"
        protocolMapper = "oidc-usermodel-attribute-mapper"
        config = @{
            "user.attribute" = "linkedeye_site_ids"
            "claim.name" = "linkedeye_site_ids"
            "jsonType.label" = "String"
            "id.token.claim" = "true"
            "access.token.claim" = "true"
            "userinfo.token.claim" = "true"
        }
    },
    @{
        name = "linkedeye-sites"
        protocol = "openid-connect"
        protocolMapper = "oidc-usermodel-attribute-mapper"
        config = @{
            "user.attribute" = "linkedeye_sites"
            "claim.name" = "linkedeye_sites"
            "jsonType.label" = "String"
            "id.token.claim" = "true"
            "access.token.claim" = "true"
            "userinfo.token.claim" = "true"
        }
    },
    @{
        name = "linkedeye-subsites"
        protocol = "openid-connect"
        protocolMapper = "oidc-usermodel-attribute-mapper"
        config = @{
            "user.attribute" = "linkedeye_subsites"
            "claim.name" = "linkedeye_subsites"
            "jsonType.label" = "String"
            "id.token.claim" = "true"
            "access.token.claim" = "true"
            "userinfo.token.claim" = "true"
        }
    }
)

foreach ($mapper in $claimMappers) {
    $mapperBody = $mapper | ConvertTo-Json -Depth 5
    try {
        Invoke-RestMethod -Method Post -Uri "$kcBase/admin/realms/$realm/clients/$clientUuid/protocol-mappers/models" -Headers $headers -Body $mapperBody | Out-Null
        Write-Host "Mapper $($mapper.name) created"
    } catch {
        Write-Host "Mapper $($mapper.name) exists or update skipped"
    }
}

$secret = (Invoke-RestMethod -Method Get -Uri "$kcBase/admin/realms/$realm/clients/$clientUuid/client-secret" -Headers $headers).value
Write-Host "Client secret: $secret"
Write-Host ""
Write-Host "Keycloak user onboarding attributes (Users -> Attributes):"
Write-Host "  linkedeye_site_ids = 6,7,8                 # lesite.id values"
Write-Host "  linkedeye_sites = fs-le-dev1,fs-le-dev2    # lesite.sitename values"
Write-Host '  linkedeye_subsites = {"fs-le-dev1":["sub1","sub2"]}   # optional JSON'
Write-Host "Assign realm roles: Admin, ViewOnly, Management, etc."
Write-Host "Done. Set KEYCLOAK_ENABLED=true and KEYCLOAK_CLIENT_SECRET in .env"
