// Aprio Strata Azure deployment.
// Targets a single resource group. Provisions:
//   - User-assigned managed identity (the workload identity)
//   - Log Analytics workspace + Application Insights
//   - Key Vault (RBAC mode)
//   - Storage Account + private blob container
//   - Azure SQL Server + database with Entra-only auth
//   - Azure Container Registry
//   - Container Apps Environment
//   - One Container App (`api`)
// Worker container, Service Bus, AI Search, and Front Door are deferred to
// the Tier 2 PR.

targetScope = 'resourceGroup'

@minLength(2)
@maxLength(10)
@description('Short environment name (e.g. dev, stg, prod). Used as a suffix.')
param environmentName string

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Object id of the Entra group/user that should be Azure SQL admin and Key Vault administrator. Pass via `azd env set AZURE_PRINCIPAL_ID`.')
param principalId string

@description('Whether the principalId belongs to a User or Group.')
@allowed([ 'User', 'Group', 'ServicePrincipal' ])
param principalType string = 'User'

@description('Container image tag for the api app. azd sets this from the build output.')
param apiImageName string = ''

@description('Optional: override the default name of the storage blob container.')
param blobContainerName string = 'firm-documents'

// AZD passes these from the Strata service config in azure.yaml.
@description('Microsoft Entra tenant id used to validate API tokens.')
param entraTenantId string = subscription().tenantId

@description('Microsoft Entra client id (app registration) for the Strata API.')
param entraClientId string = ''

@description('Microsoft Entra API audience expected on access tokens.')
param entraApiAudience string = ''

@description('STRATA_AI_PROVIDER value: azure_openai | ollama.')
param strataAiProvider string = 'azure_openai'

@description('Azure OpenAI endpoint (https://<name>.openai.azure.com).')
param azureOpenAiEndpoint string = ''

@description('Azure OpenAI deployment name (chat completions).')
param azureOpenAiDeployment string = ''

@description('Azure OpenAI API version.')
param azureOpenAiApiVersion string = '2024-10-21'

var resourceToken = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))
var tags = {
  'azd-env-name': environmentName
  'app': 'aprio-strata'
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-strata-${resourceToken}'
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Observability
// ---------------------------------------------------------------------------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-strata-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-strata-${resourceToken}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Key Vault
// ---------------------------------------------------------------------------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: take('kv-strata-${resourceToken}', 24)
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 30
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

// ---------------------------------------------------------------------------
// Storage Account + private blob container
// ---------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('ststrata${resourceToken}', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true // azd build/uploads use this; can be tightened later
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 14 }
    containerDeleteRetentionPolicy: { enabled: true, days: 14 }
  }
}

resource blobContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: blobContainerName
  properties: { publicAccess: 'None' }
}

// ---------------------------------------------------------------------------
// Azure SQL Server + database (Entra-only auth)
// ---------------------------------------------------------------------------

resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: 'sql-strata-${resourceToken}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      principalType: principalType
      login: 'aad-admin'
      sid: principalId
      tenantId: subscription().tenantId
      azureADOnlyAuthentication: true
    }
  }
}

resource sqlAllowAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sqlServer
  name: 'AllowAllAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: 'strata'
  location: location
  tags: tags
  sku: {
    name: 'GP_S_Gen5_2'
    tier: 'GeneralPurpose'
    family: 'Gen5'
    capacity: 2
  }
  properties: {
    collation: 'SQL_Latin1_General_CP1_CI_AS'
    autoPauseDelay: 60
    minCapacity: json('0.5')
    zoneRedundant: false
    requestedBackupStorageRedundancy: 'Local'
  }
}

// ---------------------------------------------------------------------------
// Container Registry + Container Apps environment
// ---------------------------------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: take('crstrata${resourceToken}', 50)
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

resource acaEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-strata-${resourceToken}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
  }
}

// ---------------------------------------------------------------------------
// RBAC role assignments to the user-assigned identity
// ---------------------------------------------------------------------------

// Built-in role IDs.
var roleStorageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var roleKeyVaultSecretsUser = '4633458b-17de-408a-b874-0445c86b69e6'
var roleAcrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource raBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, identity.id, roleStorageBlobDataContributor)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleStorageBlobDataContributor)
  }
}

resource raKv 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, identity.id, roleKeyVaultSecretsUser)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultSecretsUser)
  }
}

resource raAcr 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, roleAcrPull)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleAcrPull)
  }
}

// Also grant the deploying principal Key Vault Administrator so they can
// seed secrets during/after `azd up`.
var roleKeyVaultAdministrator = '00482a5a-887f-4fb3-b363-3b7fe8e74483'
resource raKvAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, principalId, roleKeyVaultAdministrator)
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleKeyVaultAdministrator)
  }
}

// ---------------------------------------------------------------------------
// Container App: api
// ---------------------------------------------------------------------------

var defaultImage = 'mcr.microsoft.com/k8se/quickstart:latest'
var apiImage = empty(apiImageName) ? defaultImage : apiImageName

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-strata-api-${resourceToken}'
  location: location
  tags: union(tags, { 'azd-service-name': 'api' })
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: acaEnv.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8765
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: '${registry.name}.azurecr.io'
          identity: identity.id
        }
      ]
      secrets: []
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8765 }
              initialDelaySeconds: 15
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: { path: '/readyz', port: 8765 }
              initialDelaySeconds: 10
              periodSeconds: 15
              failureThreshold: 3
            }
          ]
          env: [
            { name: 'PORT', value: '8765' }
            // DefaultAzureCredential / msodbcsql18 use AZURE_CLIENT_ID to pick
            // the right user-assigned managed identity when more than one is
            // attached. Set to the workload MI's clientId.
            { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'AZURE_SQL_SERVER', value: sqlServer.properties.fullyQualifiedDomainName }
            { name: 'AZURE_SQL_DATABASE', value: sqlDatabase.name }
            { name: 'AZURE_STORAGE_ACCOUNT_URL', value: storage.properties.primaryEndpoints.blob }
            { name: 'AZURE_STORAGE_CONTAINER', value: blobContainerName }
            // Strata API app registration (Entra ID) used for JWT audience + MSAL.
            { name: 'AZURE_TENANT_ID', value: entraTenantId }
            { name: 'AZURE_API_CLIENT_ID', value: entraClientId }
            { name: 'AZURE_API_AUDIENCE', value: empty(entraApiAudience) ? 'api://${entraClientId}' : entraApiAudience }
            { name: 'AZURE_API_SCOPE', value: 'api://${entraClientId}/access_as_user' }
            // MSAL redirect URI must be the *deployed* origin, not localhost.
            // We can't reference apiApp.properties.configuration.ingress.fqdn
            // here (self-reference), so build it from the env's default domain.
            { name: 'AZURE_REDIRECT_URI', value: 'https://ca-strata-api-${resourceToken}.${acaEnv.properties.defaultDomain}/' }
            { name: 'STRATA_AI_PROVIDER', value: strataAiProvider }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_DEPLOYMENT', value: azureOpenAiDeployment }
            { name: 'AZURE_OPENAI_API_VERSION', value: azureOpenAiApiVersion }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 5
        rules: [
          {
            name: 'http-scale'
            http: { metadata: { concurrentRequests: '50' } }
          }
        ]
      }
    }
  }
  dependsOn: [
    raAcr
    raBlob
    raKv
  ]
}

// ---------------------------------------------------------------------------
// Outputs (consumed by azd / CI to wire up follow-up tasks)
// ---------------------------------------------------------------------------

output AZURE_RESOURCE_GROUP string = resourceGroup().name
output AZURE_LOCATION string = location
output AZURE_CLIENT_ID_WORKLOAD string = identity.properties.clientId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = '${registry.name}.azurecr.io'
output AZURE_CONTAINER_REGISTRY_NAME string = registry.name
output AZURE_CONTAINER_APPS_ENVIRONMENT_ID string = acaEnv.id
output AZURE_KEY_VAULT_NAME string = keyVault.name
output AZURE_KEY_VAULT_ENDPOINT string = keyVault.properties.vaultUri
output AZURE_STORAGE_ACCOUNT_URL string = storage.properties.primaryEndpoints.blob
output AZURE_STORAGE_CONTAINER string = blobContainerName
output AZURE_SQL_SERVER string = sqlServer.properties.fullyQualifiedDomainName
output AZURE_SQL_DATABASE string = sqlDatabase.name
output APPLICATIONINSIGHTS_CONNECTION_STRING string = appInsights.properties.ConnectionString
output API_FQDN string = apiApp.properties.configuration.ingress.fqdn
output API_URL string = 'https://${apiApp.properties.configuration.ingress.fqdn}'
