// ABOUTME: The attribute keys proxy.py sets that the workshop's span registry does not declare yet. They
// ABOUTME: are written by hand until the registry adds them, then come from attributes_gen.go instead.
// SPDX-License-Identifier: Apache-2.0
package semconv

import "go.opentelemetry.io/otel/attribute"

const (
	// GenAiRequestModelKey is gen_ai.request.model, on the /chat root, chat and sanitize spans and the
	// cost gauge.
	GenAiRequestModelKey = attribute.Key("gen_ai.request.model")
	// GenAiProviderNameKey is gen_ai.provider.name, on the cost gauge. Datadog tags the gauge's provider
	// "N/A" without it.
	GenAiProviderNameKey = attribute.Key("gen_ai.provider.name")
	// GenAiProviderNameAWSBedrock is the provider of every model the workshop runs.
	GenAiProviderNameAWSBedrock = "aws.bedrock"
	// ErrorTypeKey is error.type, on an agent.forward span whose call failed.
	ErrorTypeKey = attribute.Key("error.type")
)
