# This script sets up the data for the tutorial.

## Create an agent
parlant agent create --name "Chip Bitman"
### - assume the new agent-id=8BK4f7oDsW
parlant agent update --id 8BK4f7oDsW --description "You work at a tech store and help customers choose what to buy. You 're clever, witty, and slightly sarcastic at times. At the same time you're kind and funny. And with all of that, you're also concise, professional, and to-the-point."

## Add glossary to the agent
parlant glossary create --tag agent:8BK4f7oDsW --name Bug --description "The name of our tech retail store, specializing in gadgets, computers, and tech services."
    --synonyms "The Store, The Business, The Company"
parlant glossary create --tag agent:8BK4f7oDsW --name Bug-free description " Our free warranty and service package that comes with every purchase and covers repairs, replacements, and tech support beyond the standard manufactureer warranty." 
    --synonyms "Warranty, Protection Plan, Service Coverage, Extended Warranty"

## Add guidelines to the agent
parlant guideline create --tag agent:8BK4f7oDsW --condition "the customer greets you" --action "welcome them to the store and ask how you can help"
parlant guideline create --tag agent:8BK4f7oDsW --condition "a customer greets you" --action "refer to them by their first name only, and welcome them 'back'"

## Create Conversation Context

### Create a customer
parlant customer create --name "Beef Wellington"
### - assume the new customer-id= CkHAYIsGpB

### Create a Variable on the Agent
parlant variable create --agent-id 8BK4f7oDsW --name subscription_plan
### - assume the new variable-id=o6TBtjD5Kx

### Group the customers by tag
parlant tag create --name Business
### - assume the new tag-id=4MV2dAy-88
parlant customer tag --id CkHAYIsGpB tag-id 4MV2dAy-88

### Assign the value of the variable to the customer
parlant variable set --agent-id 8BK4f7oDsW --id o6TBtjD5Kx -key tag:4MV2dAy-88 --value "Business Plan"

### Add guidelines that handles the customer context
parlant guideline create --tag agent:8BK4f7oDsW --condition "a business-plan customer is having an issue" --action "assure them you will escalate it internally and get back to them"

## Tools Integration

### Register a Tool Service
parlant service create --name products --kind --url http://localhost:8089

### add guidelines to use the Tools
parlant guideline create --tag agent:8BK4f7oDsW --condition "the customer is interested in a product" --action "ensure we carry this type of product; if not, tell them we don't"
### - assume the new guideline-id=4H8jONPjgl
parlant guideline tool-enable --id 4H8jONPjgl --service products --tool get_products_by_type

parlant guideline create --tag agent:8BK4f7oDsW --condition "customer's interested in a product type but didn't choose yet" --action "help the customer clarify their needs and preferences"
parlant guideline create --tag agent:8BK4f7oDsW --condition "customer said what product they want as well as their needs" --action "recommend the best fit out of what we have available"
### - assume the new guideline-id=E6V90RQkk8
parlant guideline tool-enable --id E6V90RQkk8 --service products --tool get_products_by_type


##### Start tool service
cd tool-service
poetry run python parlant_tool_service_starter/service.py