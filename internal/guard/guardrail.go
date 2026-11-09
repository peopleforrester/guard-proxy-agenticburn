// ABOUTME: The code-of-conduct check's Bedrock guardrail (#81): a /chat prompt the terms pass is sent to
// ABOUTME: ApplyGuardrail as source INPUT and refused when the guardrail intervenes; it fails open.
// SPDX-License-Identifier: Apache-2.0
package guard

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime"
	"github.com/aws/aws-sdk-go-v2/service/bedrockruntime/types"
)

// GuardrailTimeout bounds one ApplyGuardrail call. The spike measured 466 ms median from outside the
// cluster, 327 ms of it Bedrock's; past this the prompt goes on, because the terms still hold.
const GuardrailTimeout = 1500 * time.Millisecond

// Guardrail is a Bedrock guardrail the /chat prompt is checked against, for the abuse no term list sees:
// hate speech without a slur, insults, threats (Unleash_an_Agent_Watch_It_Burn#1267 chose Bedrock
// Guardrails, Standard tier; #1279 is the guardrail and guard-proxy's IAM). A nil Guardrail checks nothing.
type Guardrail struct {
	client      *bedrockruntime.Client
	id, version string
}

// NewGuardrail is the guardrail id at version, with the AWS SDK's own region, credentials and endpoint
// (AWS_REGION, EKS Pod Identity, AWS_ENDPOINT_URL_BEDROCK_RUNTIME). An empty id is no guardrail. A call is
// made once, not retried: within GuardrailTimeout a retry would mostly wait.
func NewGuardrail(ctx context.Context, id, version string) (*Guardrail, error) {
	if id == "" {
		return nil, nil
	}
	cfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRetryMaxAttempts(1))
	if err != nil {
		return nil, fmt.Errorf("conduct guardrail: loading the AWS configuration: %w", err)
	}
	if cfg.Region == "" {
		return nil, errors.New("conduct guardrail: CONDUCT_GUARDRAIL_ID is set without AWS_REGION or AWS_DEFAULT_REGION")
	}
	return &Guardrail{client: bedrockruntime.NewFromConfig(cfg), id: id, version: version}, nil
}

// Blocks reports whether the guardrail intervenes on text, sent as source INPUT. An error means it gave
// no verdict within GuardrailTimeout or ctx's deadline, whichever is sooner; the caller lets the text
// through. A nil Guardrail never blocks.
func (g *Guardrail) Blocks(ctx context.Context, text string) (bool, error) {
	if g == nil {
		return false, nil
	}
	ctx, cancel := context.WithTimeout(ctx, GuardrailTimeout)
	defer cancel()
	out, err := g.client.ApplyGuardrail(ctx, &bedrockruntime.ApplyGuardrailInput{
		GuardrailIdentifier: aws.String(g.id),
		GuardrailVersion:    aws.String(g.version),
		Source:              types.GuardrailContentSourceInput,
		Content: []types.GuardrailContentBlock{
			&types.GuardrailContentBlockMemberText{Value: types.GuardrailTextBlock{Text: aws.String(text)}},
		},
	})
	if err != nil {
		return false, err
	}
	return out.Action == types.GuardrailActionGuardrailIntervened, nil
}
